"""
Tests for the money and stock logic.

Run locally with the SQLite database (leave DATABASE_URL unset so tests never touch Neon):

    python manage.py test seller_app
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .models import Customer, Product, Sale, SaleItem, Seller

PASSWORD = 'Str0ng!pass9'


def make_seller(phone='08011111111'):
    return Seller.objects.create_user(phone_number=phone, password=PASSWORD)


class BaseCase(TestCase):
    def setUp(self):
        self.seller = make_seller()
        self.client.force_login(self.seller)
        self.shirt = Product.objects.create(seller=self.seller, name='Shirt', price=Decimal('2000'), stock=10)
        self.cap = Product.objects.create(seller=self.seller, name='Cap', price=Decimal('500'), stock=5)

    def sell(self, items, name='Joy', phone='08099999999', paid='0'):
        """items: list of (product, quantity)."""
        return self.client.post(reverse('add_sale'), {
            'customer_name': name,
            'customer_phone': phone,
            'amount_paid': paid,
            'product_id': [p.id for p, _ in items],
            'quantity': [q for _, q in items],
        })


class RegistrationTests(TestCase):
    def post(self, **overrides):
        data = {'username': 'Joy', 'business_name': 'Joy Hub', 'phone_number': '08022222222',
                'password': PASSWORD, 'confirm_password': PASSWORD}
        data.update(overrides)
        return self.client.post(reverse('register'), data)

    def test_register_with_matching_passwords(self):
        self.post()
        self.assertTrue(Seller.objects.filter(phone_number='08022222222').exists())

    def test_mismatched_passwords_are_rejected(self):
        self.post(confirm_password='Different!pass1')
        self.assertFalse(Seller.objects.exists())

    def test_missing_confirmation_is_rejected(self):
        self.post(confirm_password='')
        self.assertFalse(Seller.objects.exists())

    def test_phone_is_normalised(self):
        self.post(phone_number='+234 802 222 2222')
        self.assertTrue(Seller.objects.filter(phone_number='08022222222').exists())


class SaleTests(BaseCase):
    def test_multi_item_sale_deducts_stock_and_totals(self):
        self.sell([(self.shirt, 2), (self.cap, 3)], paid='1000')
        sale = Sale.objects.get()
        self.assertEqual(sale.items.count(), 2)
        self.assertEqual(sale.total_amount, Decimal('5500'))  # 2*2000 + 3*500
        self.assertEqual(sale.balance_due, Decimal('4500'))
        self.shirt.refresh_from_db(); self.cap.refresh_from_db()
        self.assertEqual((self.shirt.stock, self.cap.stock), (8, 2))

    def test_same_product_on_two_rows_is_merged(self):
        self.sell([(self.shirt, 1), (self.shirt, 2)])
        sale = Sale.objects.get()
        self.assertEqual(sale.items.count(), 1)
        self.assertEqual(sale.items.get().quantity, 3)
        self.shirt.refresh_from_db()
        self.assertEqual(self.shirt.stock, 7)

    def test_not_enough_stock_rejects_whole_sale(self):
        self.sell([(self.shirt, 2), (self.cap, 99)])
        self.assertFalse(Sale.objects.exists())
        self.shirt.refresh_from_db(); self.cap.refresh_from_db()
        self.assertEqual((self.shirt.stock, self.cap.stock), (10, 5))  # nothing deducted

    def test_overpayment_is_rejected(self):
        self.sell([(self.cap, 1)], paid='600')
        self.assertFalse(Sale.objects.exists())
        self.cap.refresh_from_db()
        self.assertEqual(self.cap.stock, 5)

    def test_invalid_phone_is_rejected(self):
        self.sell([(self.cap, 1)], phone='123')
        self.assertFalse(Sale.objects.exists())

    def test_no_product_chosen_is_rejected(self):
        response = self.client.post(reverse('add_sale'), {
            'customer_name': 'Joy', 'amount_paid': '0', 'product_id': [''], 'quantity': ['1']})
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(response.status_code, 200)

    def test_customer_is_reused_and_phone_saved(self):
        self.sell([(self.cap, 1)], name='Joy', phone='08099999999')
        self.sell([(self.cap, 1)], name='joy', phone='')
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Customer.objects.get().phone_number, '08099999999')

    def test_cannot_sell_another_sellers_product(self):
        other = make_seller('08033333333')
        theirs = Product.objects.create(seller=other, name='Theirs', price=Decimal('100'), stock=5)
        self.sell([(theirs, 1)])
        self.assertFalse(Sale.objects.exists())
        theirs.refresh_from_db()
        self.assertEqual(theirs.stock, 5)


class DebtTests(BaseCase):
    def test_payment_clears_oldest_sale_first(self):
        self.sell([(self.shirt, 1)], paid='0')   # owes 2000 (older)
        self.sell([(self.cap, 2)], paid='0')     # owes 1000
        customer = Customer.objects.get()
        self.client.post(reverse('record_payment', args=[customer.pk]), {'amount': '2500'})
        oldest, newest = Sale.objects.order_by('date', 'id')
        self.assertEqual(oldest.balance_due, Decimal('0'))
        self.assertEqual(newest.balance_due, Decimal('500'))

    def test_debts_page_lists_debtor_and_hides_cleared(self):
        self.sell([(self.shirt, 1)], paid='0')
        self.assertContains(self.client.get(reverse('debts')), 'Joy')
        customer = Customer.objects.get()
        self.client.post(reverse('record_payment', args=[customer.pk]), {'amount': '2000'})
        self.assertContains(self.client.get(reverse('debts')), 'All clear')


class ProductTests(BaseCase):
    def test_edit_updates_fields(self):
        self.client.post(reverse('edit_product', args=[self.shirt.pk]),
                         {'name': 'Big Shirt', 'price': '2500', 'stock': '4'})
        self.shirt.refresh_from_db()
        self.assertEqual((self.shirt.name, self.shirt.price, self.shirt.stock), ('Big Shirt', Decimal('2500'), 4))

    def test_edit_rejects_negative_stock(self):
        self.client.post(reverse('edit_product', args=[self.shirt.pk]),
                         {'name': 'Shirt', 'price': '2000', 'stock': '-1'})
        self.shirt.refresh_from_db()
        self.assertEqual(self.shirt.stock, 10)

    def test_price_change_does_not_rewrite_past_sales(self):
        self.sell([(self.shirt, 1)])
        self.client.post(reverse('edit_product', args=[self.shirt.pk]),
                         {'name': 'Shirt', 'price': '9999', 'stock': '9'})
        self.assertEqual(SaleItem.objects.get().price_at_sale, Decimal('2000'))
        self.assertEqual(Sale.objects.get().total_amount, Decimal('2000'))

    def test_delete_unsold_product(self):
        self.client.post(reverse('delete_product', args=[self.cap.pk]))
        self.assertFalse(Product.objects.filter(pk=self.cap.pk).exists())

    def test_sold_product_cannot_be_deleted(self):
        self.sell([(self.cap, 1)])
        self.client.post(reverse('delete_product', args=[self.cap.pk]))
        self.assertTrue(Product.objects.filter(pk=self.cap.pk).exists())

    def test_cannot_edit_or_delete_another_sellers_product(self):
        other = make_seller('08033333333')
        theirs = Product.objects.create(seller=other, name='Theirs', price=Decimal('100'), stock=5)
        self.assertEqual(self.client.get(reverse('edit_product', args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('delete_product', args=[theirs.pk])).status_code, 404)
        self.assertTrue(Product.objects.filter(pk=theirs.pk).exists())


class VoidSaleTests(BaseCase):
    def test_void_restores_stock_and_removes_sale(self):
        self.sell([(self.shirt, 2), (self.cap, 3)], paid='500')
        sale = Sale.objects.get()
        self.client.post(reverse('void_sale', args=[sale.pk]))
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleItem.objects.exists())
        self.shirt.refresh_from_db(); self.cap.refresh_from_db()
        self.assertEqual((self.shirt.stock, self.cap.stock), (10, 5))

    def test_void_requires_post(self):
        self.sell([(self.cap, 1)])
        sale = Sale.objects.get()
        self.assertEqual(self.client.get(reverse('void_sale', args=[sale.pk])).status_code, 405)
        self.assertTrue(Sale.objects.exists())

    def test_cannot_void_another_sellers_sale(self):
        other = make_seller('08033333333')
        sale = Sale.objects.create(seller=other, total_amount=Decimal('100'))
        self.assertEqual(self.client.post(reverse('void_sale', args=[sale.pk])).status_code, 404)
        self.assertTrue(Sale.objects.filter(pk=sale.pk).exists())

    def test_voided_sale_no_longer_counts_as_debt(self):
        self.sell([(self.shirt, 1)], paid='0')
        self.client.post(reverse('void_sale', args=[Sale.objects.get().pk]))
        self.assertContains(self.client.get(reverse('debts')), 'All clear')


class CustomerTests(BaseCase):
    def test_save_contact(self):
        self.client.post(reverse('add_customer'), {'name': 'Ada', 'phone_number': '08055555555'})
        self.assertEqual(Customer.objects.get().phone_number, '08055555555')

    def test_save_contact_requires_valid_phone(self):
        self.client.post(reverse('add_customer'), {'name': 'Ada', 'phone_number': '55'})
        self.assertFalse(Customer.objects.exists())

    def test_non_debtor_appears_in_people(self):
        self.sell([(self.cap, 1)], paid='500')  # paid in full, no debt
        self.assertContains(self.client.get(reverse('customers')), 'Joy')

    def test_update_customer_phone(self):
        c = Customer.objects.create(seller=self.seller, name='Ada')
        self.client.post(reverse('update_customer', args=[c.pk]), {'name': 'Ada', 'phone_number': '08055555555'})
        c.refresh_from_db()
        self.assertEqual(c.phone_number, '08055555555')

    def test_cannot_update_another_sellers_customer(self):
        other = make_seller('08033333333')
        c = Customer.objects.create(seller=other, name='Theirs')
        self.assertEqual(self.client.post(reverse('update_customer', args=[c.pk]),
                                          {'name': 'Hacked', 'phone_number': ''}).status_code, 404)


class PageSmokeTests(BaseCase):
    def test_every_page_loads_for_a_logged_in_seller(self):
        self.sell([(self.cap, 1)])
        sale = Sale.objects.get()
        customer = Customer.objects.get()
        for name, args in [('dashboard', []), ('stock', []), ('add_sale', []), ('sales', []),
                           ('customers', []), ('debts', []),
                           ('edit_product', [self.shirt.pk])]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        self.assertEqual(self.client.get(reverse('sales') + '?q=joy').status_code, 200)
        self.assertEqual(self.client.get(reverse('customers') + '?q=joy').status_code, 200)

    def test_pages_require_login(self):
        self.client.logout()
        for name in ['dashboard', 'stock', 'add_sale', 'sales', 'customers', 'debts']:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_auth_pages_show_password_toggle(self):
        self.client.logout()
        self.assertContains(self.client.get(reverse('login')), 'data-toggle-pw')
        self.assertContains(self.client.get(reverse('register')), 'confirm_password')
