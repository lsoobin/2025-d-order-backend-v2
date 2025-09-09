from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils.timezone import now
from django.utils import timezone
from datetime import timedelta
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from order.models import *
from menu.models import *
from booth.models import *
from manager.models import *
from cart.models import *
from order.serializers import *
from coupon.models import *

def get_table_fee_and_type_by_booth(booth_id: int):
    m = Manager.objects.filter(booth_id=booth_id).first()
    if not m:
        return 0, "none"
    if m.seat_type == "PP":
        return int(m.seat_tax_person or 0), "person"
    if m.seat_type == "PT":
        return int(m.seat_tax_table or 0), "table"
    return 0, "none"


def is_first_order_for_table_session(table_id: int, booth_id: int, now_dt):
    table = Table.objects.filter(pk=table_id, booth_id=booth_id).first()
    if not table:
        return True

    entered_at = getattr(table, "entered_at", None)
    qs = Order.objects.filter(table_id=table_id)
    if entered_at:
        qs = qs.filter(created_at__gte=entered_at)

    return not qs.exists()

class OrderPasswordVerifyView(APIView):
    def post(self, request):
        booth_id = request.headers.get('Booth-ID')
        password = request.data.get('password')
        table_id = request.data.get('table_id')
        table_num = request.data.get('table_num')
        coupon_id = request.data.get('coupon_id')
        now_dt = timezone.now()

        if not booth_id or not str(booth_id).isdigit():
            return Response({"status": "error", "code": 404, "message": "Booth-ID가 누락되었거나 잘못되었습니다."}, status=404)
        booth = Booth.objects.filter(pk=int(booth_id)).first()
        if not booth:
            return Response({"status": "error", "code": 404, "message": "해당 Booth가 존재하지 않습니다."}, status=404)

        manager = Manager.objects.filter(booth=booth).first()
        if not manager:
            return Response({"status": "error", "code": 404, "message": "해당 부스의 운영자 정보가 없습니다."}, status=404)

        if not password or not str(password).isdigit() or len(str(password)) != 4:
            return Response({"status": "error", "code": 400, "message": "비밀번호는 4자리 숫자여야 합니다."}, status=400)
        if str(password) != str(manager.order_check_password):
            return Response({"status": "error", "code": 401, "message": "비밀번호가 일치하지 않습니다."}, status=401)

        if table_id:
            table = Table.objects.filter(pk=table_id, booth=booth).first()
        elif table_num is not None:
            table = Table.objects.filter(table_num=table_num, booth=booth).first()
        else:
            return Response({"status": "error", "code": 400, "message": "table_id 또는 table_num이 필요합니다."}, status=400)

        if not table:
            return Response({"status": "error", "code": 404, "message": "해당 테이블을 찾을 수 없습니다."}, status=404)

        cart = Cart.objects.filter(table_id=table.id, is_ordered=False).first()
        if not cart:
            return Response({"status": "error", "code": 404, "message": "주문 가능한 장바구니가 없습니다."}, status=404)

        cart_menus = list(CartMenu.objects.filter(cart=cart))
        cart_sets = list(CartSetMenu.objects.filter(cart=cart))
        if not cart_menus and not cart_sets:
            return Response({"status": "error", "code": 400, "message": "장바구니가 비어 있습니다."}, status=400)

        try:
            with transaction.atomic():
                order = Order.objects.create(
                    table_id=table.id,
                    order_status="pending",
                    order_amount=0,
                )

                subtotal = 0

                for cm in cart_menus:
                    menu = get_object_or_404(Menu, pk=cm.menu_id)
                    if menu.menu_amount < cm.quantity:
                        raise ValueError(f"'{menu.menu_name}' 재고 부족")
                    menu.menu_amount -= cm.quantity
                    menu.save()

                    OrderMenu.objects.create(
                        order=order,
                        menu=menu,
                        quantity=cm.quantity,
                        fixed_price=menu.menu_price,
                    )
                    subtotal += menu.menu_price * cm.quantity

                for cs in cart_sets:
                    setmenu = get_object_or_404(SetMenu, pk=cs.set_menu_id)
                    sm_items = SetMenuItem.objects.filter(set_menu_id=setmenu.pk)

                    for smi in sm_items:
                        need = smi.quantity * cs.quantity
                        mobj = get_object_or_404(Menu, pk=smi.menu_id)
                        if mobj.menu_amount < need:
                            raise ValueError(f"세트 '{setmenu.set_name}' 구성 '{mobj.menu_name}' 재고 부족")
                    for smi in sm_items:
                        need = smi.quantity * cs.quantity
                        mobj = get_object_or_404(Menu, pk=smi.menu_id)
                        mobj.menu_amount -= need
                        mobj.save()

                    OrderSetMenu.objects.create(
                        order=order,
                        set_menu=setmenu,
                        quantity=cs.quantity,
                        fixed_price=setmenu.set_price,
                    )
                    subtotal += setmenu.set_price * cs.quantity

                table_fee = 0
                if is_first_order_for_table_session(table_id=table.id, booth_id=booth.id, now_dt=now_dt):
                    base_fee, seat_mode = get_table_fee_and_type_by_booth(booth.id)
                    if seat_mode == "person":
                        person_qty = request.data.get("people_count", 0)
                        table_fee = max(0, int(base_fee)) * int(person_qty)
                    elif seat_mode == "table":
                        table_fee = max(0, int(base_fee))

                coupon_applied = False
                coupon_discount = 0
                coupon_info = None
                if coupon_id is not None:
                    coupon = Coupon.objects.filter(pk=coupon_id, booth_id=booth.id).first()
                    if not coupon:
                        return Response({"status": "error", "code": 404, "message": "쿠폰을 찾을 수 없습니다."}, status=404)
                    if (coupon.quantity or 0) <= 0:
                        return Response({"status": "error", "code": 400, "message": "해당 쿠폰은 더 이상 사용할 수 없습니다."}, status=400)
                    pre_discount_total = subtotal + table_fee
                    dtype = (coupon.discount_type or "").lower()
                    dval = int(coupon.discount_value or 0)
                    if dtype in ("percent", "percentage", "pct"):
                        pct = max(0, min(100, dval))
                        coupon_discount = (pre_discount_total * pct) // 100
                    elif dtype in ("amount", "fixed", "won"):
                        coupon_discount = max(0, dval)
                    else:
                        return Response({"status": "error", "code": 400, "message": "알 수 없는 쿠폰 타입입니다."}, status=400)
                    coupon_discount = min(coupon_discount, pre_discount_total)
                    coupon_applied = coupon_discount > 0
                    coupon_info = {"coupon_id": coupon.id, "coupon_name": coupon.coupon_name}

                order_amount = subtotal + table_fee - coupon_discount
                if order_amount < 0:
                    order_amount = 0
                order.order_amount = order_amount
                order.save()

                if coupon_applied:
                    coupon.quantity = (coupon.quantity or 0) - 1
                    coupon.save()
                    TableCoupon.objects.create(
                        table_id=table.id,
                        coupon_id=coupon.id,
                        used_at=now_dt
                    )

                booth.total_revenues = (booth.total_revenues or 0) + order_amount
                booth.save()

                CartMenu.objects.filter(cart=cart).delete()
                CartSetMenu.objects.filter(cart=cart).delete()
                cart.is_ordered = True
                cart.save()

                # 주문 성공 후 WebSocket 브로드캐스트 추가
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f"booth_{booth.id}_orders",
                    {
                        "type": "new_order",
                        "data": {
                            "order_id": order.pk,
                            "table_num": table.table_num,
                            "items": [
                                {"menu_name": cm.menu.menu_name, "quantity": cm.quantity}
                                for cm in cart_menus
                            ] + [
                                {"set_name": cs.set_menu.set_name, "quantity": cs.quantity}
                                for cs in cart_sets
                            ],
                            "order_amount": order.order_amount
                        }
                    }
                )

                return Response({
                    "status": "success",
                    "code": 201,
                    "message": "주문이 생성되었습니다.",
                    "data": {
                        "order_id": order.pk,
                        "order_amount": order.order_amount,
                        "subtotal": subtotal,
                        "table_fee": table_fee,
                        "coupon_discount": coupon_discount,
                        "coupon": coupon_info,
                        "booth_total_revenues": booth.total_revenues
                    }
                }, status=201)

        except ValueError as e:
            return Response({"status": "error", "code": 400, "message": str(e)}, status=400)
        except Exception:
            return Response({"status": "error", "code": 500, "message": "주문 생성 중 오류가 발생했습니다."}, status=500)


class TableOrderListView(APIView):
    def get(self, request, table_num):
        booth_id = request.headers.get("Booth-ID")
        if not booth_id:
            return Response({"status": "error", "code": 400, "message": "Booth-ID header is required."}, status=400)

        booth = Booth.objects.filter(pk=booth_id).first()
        if not booth:
            return Response({"status": "error", "code": 404, "message": "해당 부스를 찾을 수 없습니다."}, status=404)

        table = Table.objects.filter(booth=booth, table_num=table_num).first()
        if not table:
            return Response({"status": "error", "code": 404, "message": "해당 테이블을 찾을 수 없습니다."}, status=404)

        entered_at = getattr(table, "entered_at", None)
        valid_orders = Order.objects.filter(table=table)
        if entered_at:
            valid_orders = valid_orders.filter(created_at__gte=entered_at)

        order_menus = OrderMenu.objects.filter(order__in=valid_orders).select_related("menu", "order")
        order_set_menus = OrderSetMenu.objects.filter(order__in=valid_orders).select_related("set_menu", "order")

        expanded = []

        for om in order_menus:
            row = OrderMenuSerializer(om).data
            row["from_set"] = False
            if isinstance(row.get("created_at"), timezone.datetime):
                row["created_at"] = row["created_at"].isoformat()
            expanded.append(row)

        for osm in order_set_menus:
            for smi in SetMenuItem.objects.filter(set_menu_id=osm.set_menu_id).select_related("menu"):
                expanded.append({
                    "id": None,
                    "order": osm.order_id,
                    "menu": smi.menu_id,
                    "menu_name": smi.menu.menu_name,
                    "fixed_price": smi.menu.menu_price,
                    "quantity": smi.quantity * osm.quantity,
                    "created_at": osm.created_at.isoformat(),
                    "from_set": True,
                    "set_id": osm.set_menu_id,
                    "set_name": osm.set_menu.set_name,
                })

        expanded.sort(key=lambda x: x["created_at"])

        return Response({"status": "success", "code": 200, "data": {"orders": expanded}}, status=200)

class CallStaffAPIView(APIView):
    def post(self, request):
        table_num = request.data.get("table_num")
        message = request.data.get("message", "직원 호출")
        booth_id = request.headers.get("Booth-ID")

        if not table_num:
            return Response(
                {"message": "table_num 값이 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not booth_id:
            return Response(
                {"message": "Booth-ID 헤더가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST
            )

        table = get_object_or_404(Table, booth_id=booth_id, table_num=table_num)
        channel_layer = get_channel_layer()

        async_to_sync(channel_layer.group_send)(
            f"booth_{booth_id}_staff_calls",
            {
                "type": "staff_call",
                "tableNumber": table.table_num,
                "boothId": booth_id,
                "message": message
            }
        )

        return Response({
            "message": "직원 호출이 전송되었습니다.",
            "boothId": booth_id,
            "tableNumber": table.table_num,
            "data": {"message": message}
        }, status=status.HTTP_200_OK)
