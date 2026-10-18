import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Customer, Product, Sale, SaleItem, Seller

ZERO = Decimal('0')
MAX_MONEY = Decimal('99999999.99')


# ---------- helpers ----------
def normalize_phone(raw):
    """'+234 801 234 5678' / '2348012345678' -> '08012345678'."""
    phone = re.sub(r'[\s\-()]', '', raw or '')
    if phone.startswith('+234'):
        phone = '0' + phone[4:]
    elif phone.startswith('234') and len(phone) == 13:
        phone = '0' + phone[3:]
    return phone


def to_decimal(raw, default=None):
    try:
        value = Decimal(str(raw).strip().replace(',', ''))
    except (InvalidOperation, AttributeError):
        return default
    if not value.is_finite() or value < 0 or value > MAX_MONEY:
        return default
    return value


def to_int(raw, default=None):
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


def whatsapp_link(phone, text):
    digits = normalize_phone(phone)
    if not digits.startswith('0') or len(digits) < 10:
        return ''
    return f"https://wa.me/234{digits[1:]}?text={quote(text)}"


# ---------- auth ----------
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        raw = request.POST.get('phone_number', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, phone_number=normalize_phone(raw), password=password)
        if user is None and raw != normalize_phone(raw):
            user = authenticate(request, phone_number=raw, password=password)
        if user is not None:
            login(request, user)
            return redirect('dashboard')
        messages.error(request, 'Invalid phone number or password.')

    return render(request, 'seller_app/login.html')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = {}
    if request.method == 'POST':
        form = {
            'username': request.POST.get('username', '').strip(),
            'business_name': request.POST.get('business_name', '').strip(),
            'phone_number': normalize_phone(request.POST.get('phone_number')),
        }
        password = request.POST.get('password', '')
        phone = form['phone_number']

        error = None
        if not form['username']:
            error = 'Please enter your name.'
        elif not re.fullmatch(r'0\d{10}', phone):
            error = 'Enter a valid 11-digit Nigerian phone number, e.g. 08012345678.'
        elif Seller.objects.filter(phone_number=phone).exists():
            error = 'This phone number is already registered.'
        else:
            try:
                validate_password(password)
            except ValidationError as exc:
                error = ' '.join(exc.messages)

        if error:
            messages.error(request, error)
        else:
            user = Seller.objects.create_user(phone_number=phone, password=password)
            user.username = form['username'][:50]
            user.business_name = form['business_name'][:100]
            user.save()
            login(request, user)
            return redirect('dashboard')

    return render(request, 'seller_app/register.html', {'form': form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')


# ---------- dashboard ----------
@login_required
def dashboard(request):
    sales = Sale.objects.filter(seller=request.user)
    start_of_day = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)

    today = sales.filter(date__gte=start_of_day).aggregate(
        revenue=Sum('amount_paid'), booked=Sum('total_amount'))
    totals = sales.aggregate(total=Sum('total_amount'), paid=Sum('amount_paid'))
    owed = (totals['total'] or ZERO) - (totals['paid'] or ZERO)

    context = {
        'today_collected': today['revenue'] or ZERO,
        'today_count': sales.filter(date__gte=start_of_day).count(),
        'total_owed': owed,
        'low_stock': Product.objects.filter(
            seller=request.user, stock__lte=Product.LOW_STOCK_LEVEL).order_by('stock', 'name')[:5],
        'recent_sales': sales.select_related('customer').prefetch_related('items__product')
                             .order_by('-date')[:5],
        'product_count': Product.objects.filter(seller=request.user).count(),
    }
    return render(request, 'seller_app/dashboard.html', context)


# ---------- stock ----------
@login_required
def stock_list(request):
    products = Product.objects.filter(seller=request.user).order_by('name')
    return render(request, 'seller_app/stock.html', {'products': products})


@login_required
@require_POST
def add_product(request):
    name = request.POST.get('name', '').strip()[:100]
    price = to_decimal(request.POST.get('price'))
    stock = to_int(request.POST.get('stock', 0), 0)

    if not name or price is None:
        messages.error(request, 'Enter a product name and a valid price.')
    elif stock < 0:
        messages.error(request, 'Stock cannot be negative.')
    else:
        Product.objects.create(seller=request.user, name=name, price=price, stock=stock)
        messages.success(request, f'{name} added to your stock.')
    return redirect('stock')


@login_required
@require_POST
def update_stock(request, pk):
    with transaction.atomic():
        product = get_object_or_404(Product.objects.select_for_update(), pk=pk, seller=request.user)
        action = request.POST.get('action')
        if action == 'increase':
            product.stock += 1
        elif action == 'decrease' and product.stock > 0:
            product.stock -= 1
        product.save(update_fields=['stock'])
    # HTMX swaps in just the updated row
    return render(request, 'seller_app/partials/product_row.html', {'product': product})


# ---------- sales ----------
@login_required
def add_sale(request):
    products = Product.objects.filter(seller=request.user, stock__gt=0).order_by('name')

    if request.method == 'POST':
        customer_name = request.POST.get('customer_name', '').strip()[:100]
        customer_phone = normalize_phone(request.POST.get('customer_phone'))[:15]
        quantity = to_int(request.POST.get('quantity'), 0)
        amount_paid = to_decimal(request.POST.get('amount_paid') or '0')

        error = None
        if not customer_name:
            error = 'Enter the customer name.'
        elif quantity < 1:
            error = 'Quantity must be at least 1.'
        elif amount_paid is None:
            error = 'Enter a valid amount paid.'

        if not error:
            with transaction.atomic():
                product = get_object_or_404(
                    Product.objects.select_for_update(),
                    id=to_int(request.POST.get('product_id'), 0), seller=request.user)
                total = product.price * quantity

                if product.stock < quantity:
                    error = f'Not enough stock! Only {product.stock} left.'
                elif amount_paid > total:
                    error = f'Amount paid is more than the total (₦{total:,.2f}).'
                else:
                    customer = Customer.objects.filter(
                        seller=request.user, name__iexact=customer_name).first()
                    if customer is None:
                        customer = Customer.objects.create(
                            seller=request.user, name=customer_name, phone_number=customer_phone)
                    elif customer_phone and not customer.phone_number:
                        customer.phone_number = customer_phone
                        customer.save(update_fields=['phone_number'])

                    sale = Sale.objects.create(
                        seller=request.user, customer=customer,
                        total_amount=total, amount_paid=amount_paid)
                    # SaleItem.save() deducts the stock
                    SaleItem.objects.create(
                        sale=sale, product=product, quantity=quantity, price_at_sale=product.price)

                    owing = total - amount_paid
                    if owing > 0:
                        messages.success(
                            request, f'Sale recorded. {customer.name} still owes ₦{owing:,.2f}.')
                    else:
                        messages.success(request, f'Sale recorded. {customer.name} paid in full.')
                    return redirect('dashboard')

        messages.error(request, error)

    return render(request, 'seller_app/add_sale.html', {'products': products})


# ---------- debts ----------
@login_required
def debts_list(request):
    customers = (Customer.objects.filter(seller=request.user)
                 .annotate(owed=Sum(F('sales__total_amount') - F('sales__amount_paid')))
                 .filter(owed__gt=0).order_by('-owed'))
    debtors = list(customers)
    for c in debtors:
        c.wa_link = whatsapp_link(
            c.phone_number,
            f"Hello {c.name}, a friendly reminder that you have an outstanding balance "
            f"of ₦{c.owed:,.2f} with {request.user.business_name or request.user.username or 'us'}. Thank you!")
    total = sum((c.owed for c in debtors), ZERO)
    return render(request, 'seller_app/debts.html', {'debtors': debtors, 'total_owed': total})


@login_required
@require_POST
def record_payment(request, pk):
    customer = get_object_or_404(Customer, pk=pk, seller=request.user)
    amount = to_decimal(request.POST.get('amount'))
    if not amount:
        messages.error(request, 'Enter a valid payment amount.')
        return redirect('debts')

    with transaction.atomic():
        remaining = amount
        unpaid = (Sale.objects.select_for_update()
                  .filter(seller=request.user, customer=customer,
                          amount_paid__lt=F('total_amount')).order_by('date'))
        for sale in unpaid:
            if remaining <= 0:
                break
            applied = min(remaining, sale.total_amount - sale.amount_paid)
            sale.amount_paid += applied
            sale.save(update_fields=['amount_paid'])
            remaining -= applied

    applied_total = amount - remaining
    if remaining > 0:
        messages.success(
            request, f'₦{applied_total:,.2f} recorded. ₦{remaining:,.2f} was more than {customer.name} owed and was ignored.')
    else:
        messages.success(request, f'₦{applied_total:,.2f} payment recorded for {customer.name}.')
    return redirect('debts')
