"""
Tests for the money and stock logic.

Run locally with the SQLite database (leave DATABASE_URL unset so tests never touch Neon):

    python manage.py test seller_app
"""
import re
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .invoice import build_invoice_data
from .models import Customer, Product, ProfilePhoto, Sale, SaleItem, Seller
from .photos import PhotoError, make_avatar_sizes

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
                           ('edit_product', [self.shirt.pk]),
                           ('invoice', [sale.pk]), ('invoice_pdf', [sale.pk]), ('invoice_jpg', [sale.pk])]:
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


class InvoiceTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.seller.business_name = 'Joy Fashion Hub'
        self.seller.save()
        self.sell([(self.shirt, 2), (self.cap, 3)], paid='1000')
        self.sale = Sale.objects.get()

    def test_recording_a_sale_opens_its_invoice(self):
        response = self.sell([(self.cap, 1)])
        newest = Sale.objects.order_by('-id').first()
        self.assertRedirects(response, reverse('invoice', args=[newest.pk]))

    def test_invoice_page_shows_company_date_time_and_items(self):
        response = self.client.get(reverse('invoice', args=[self.sale.pk]))
        local = timezone.localtime(self.sale.date)
        self.assertContains(response, 'Joy Fashion Hub')
        self.assertContains(response, f'INV-{self.sale.pk:05d}')
        self.assertContains(response, local.strftime('%d %b %Y'))
        self.assertContains(response, 'Shirt')
        self.assertContains(response, '4,500.00')  # balance due

    def test_pdf_download(self):
        response = self.client.get(reverse('invoice_pdf', args=[self.sale.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn(f'INV-{self.sale.pk:05d}', response['Content-Disposition'])
        self.assertIn('.pdf', response['Content-Disposition'])

    def test_jpg_download(self):
        response = self.client.get(reverse('invoice_jpg', args=[self.sale.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        image = Image.open(BytesIO(response.content))
        self.assertEqual(image.format, 'JPEG')
        self.assertEqual(image.size[0], 1000)

    def test_inline_option_opens_in_browser(self):
        response = self.client.get(reverse('invoice_pdf', args=[self.sale.pk]) + '?inline=1')
        self.assertIn('inline', response['Content-Disposition'])

    def test_invoice_data_uses_company_name_and_local_time(self):
        data = build_invoice_data(self.sale, self.seller)
        local = timezone.localtime(self.sale.date)
        self.assertEqual(data.company, 'Joy Fashion Hub')
        self.assertEqual(data.date, local.strftime('%d %b %Y'))
        self.assertEqual(data.time, local.strftime('%I:%M %p').lstrip('0'))
        self.assertEqual((len(data.lines), data.total, data.paid, data.balance),
                         (2, Decimal('5500'), Decimal('1000'), Decimal('4500')))

    def test_company_name_falls_back_to_seller_name(self):
        self.seller.business_name = ''
        self.seller.username = 'Ada'
        self.seller.save()
        self.assertEqual(build_invoice_data(self.sale, self.seller).company, 'Ada')

    def test_invoice_uses_price_at_time_of_sale(self):
        self.client.post(reverse('edit_product', args=[self.shirt.pk]),
                         {'name': 'Shirt', 'price': '9999', 'stock': '8'})
        data = build_invoice_data(Sale.objects.get(pk=self.sale.pk), self.seller)
        shirt_line = next(l for l in data.lines if l.name == 'Shirt')
        self.assertEqual(shirt_line.unit_price, Decimal('2000'))

    def test_another_seller_cannot_open_the_invoice(self):
        make_seller('08033333333')
        self.client.logout()
        self.client.force_login(Seller.objects.get(phone_number='08033333333'))
        for name in ('invoice', 'invoice_pdf', 'invoice_jpg'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=[self.sale.pk])).status_code, 404)

    def test_invoice_requires_login(self):
        self.client.logout()
        for name in ('invoice', 'invoice_pdf', 'invoice_jpg'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=[self.sale.pk])).status_code, 302)

    def test_walk_in_sale_still_renders(self):
        walk_in = Sale.objects.create(seller=self.seller, total_amount=Decimal('500'), amount_paid=Decimal('500'))
        for name in ('invoice', 'invoice_pdf', 'invoice_jpg'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=[walk_in.pk])).status_code, 200)


def picture(size=(400, 300), mode='RGB', color='blue', fmt='PNG'):
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def upload(name='me.png', data=None, content_type='image/png'):
    return SimpleUploadedFile(name, data if data is not None else picture(), content_type=content_type)


class PhotoProcessingTests(TestCase):
    def sizes_of(self, result):
        return {name: Image.open(BytesIO(data)).size for name, data in result.items()}

    def test_makes_three_square_jpeg_sizes_from_a_wide_photo(self):
        result = make_avatar_sizes(BytesIO(picture((1600, 900), fmt='JPEG')))
        self.assertEqual(self.sizes_of(result), {'small': (96, 96), 'medium': (256, 256), 'large': (640, 640)})
        for data in result.values():
            self.assertEqual(Image.open(BytesIO(data)).format, 'JPEG')

    def test_small_picture_is_not_blown_up(self):
        result = make_avatar_sizes(BytesIO(picture((120, 200))))
        self.assertEqual(self.sizes_of(result), {'small': (96, 96), 'medium': (120, 120), 'large': (120, 120)})

    def test_transparent_png_is_accepted(self):
        result = make_avatar_sizes(BytesIO(picture((300, 300), mode='RGBA', color=(0, 0, 0, 0))))
        self.assertEqual(self.sizes_of(result)['small'], (96, 96))

    def test_rejects_things_that_are_not_pictures(self):
        with self.assertRaises(PhotoError):
            make_avatar_sizes(BytesIO(b'definitely not an image'))

    def test_rejects_tiny_pictures(self):
        with self.assertRaises(PhotoError):
            make_avatar_sizes(BytesIO(picture((30, 30))))

    def test_rejects_huge_files(self):
        with self.assertRaises(PhotoError):
            make_avatar_sizes(BytesIO(b'0' * (8 * 1024 * 1024 + 10)))


class ProfileTests(BaseCase):
    def test_profile_page_loads(self):
        response = self.client.get(reverse('profile'))
        self.assertContains(response, '08011111111')

    def test_update_profile(self):
        self.client.post(reverse('profile'), {'username': 'Joy', 'business_name': 'Joy Hub', 'email': 'joy@example.com'})
        self.seller.refresh_from_db()
        self.assertEqual((self.seller.username, self.seller.business_name, self.seller.email),
                         ('Joy', 'Joy Hub', 'joy@example.com'))

    def test_invalid_email_is_rejected(self):
        self.client.post(reverse('profile'), {'username': 'Joy', 'business_name': '', 'email': 'not-an-email'})
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.email, '')

    def test_upload_stores_three_sizes_and_serves_them(self):
        self.client.post(reverse('upload_photo'), {'photo': upload(data=picture((800, 600)))})
        self.seller.refresh_from_db()
        self.assertIsNotNone(self.seller.photo_updated)
        self.assertEqual(ProfilePhoto.objects.filter(seller=self.seller).count(), 1)
        for size, pixels in (('small', 96), ('medium', 256), ('large', 600)):
            with self.subTest(size=size):
                response = self.client.get(reverse('profile_photo', args=[size]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'image/jpeg')
                self.assertEqual(Image.open(BytesIO(response.content)).size, (pixels, pixels))

    def test_uploading_again_replaces_the_photo(self):
        self.client.post(reverse('upload_photo'), {'photo': upload()})
        self.client.post(reverse('upload_photo'), {'photo': upload(data=picture((500, 500), color='red'))})
        self.assertEqual(ProfilePhoto.objects.filter(seller=self.seller).count(), 1)

    def test_non_image_upload_is_rejected(self):
        self.client.post(reverse('upload_photo'), {'photo': upload('x.png', b'not a picture')})
        self.assertFalse(ProfilePhoto.objects.exists())
        self.seller.refresh_from_db()
        self.assertIsNone(self.seller.photo_updated)

    def test_missing_file_is_handled(self):
        self.assertEqual(self.client.post(reverse('upload_photo')).status_code, 302)

    def test_unknown_size_and_missing_photo_are_404(self):
        self.assertEqual(self.client.get(reverse('profile_photo', args=['small'])).status_code, 404)
        self.client.post(reverse('upload_photo'), {'photo': upload()})
        self.assertEqual(self.client.get(reverse('profile_photo', args=['huge'])).status_code, 404)

    def test_photos_are_private_to_their_owner(self):
        self.client.post(reverse('upload_photo'), {'photo': upload()})
        other = make_seller('08033333333')
        self.client.logout()
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('profile_photo', args=['small'])).status_code, 404)

    def test_photo_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('profile_photo', args=['small'])).status_code, 302)

    def test_remove_photo(self):
        self.client.post(reverse('upload_photo'), {'photo': upload()})
        self.client.post(reverse('remove_photo'))
        self.assertFalse(ProfilePhoto.objects.exists())
        self.seller.refresh_from_db()
        self.assertIsNone(self.seller.photo_updated)

    def test_header_avatar_appears_after_upload(self):
        self.assertNotContains(self.client.get(reverse('dashboard')), 'profile/photo/small')
        self.client.post(reverse('upload_photo'), {'photo': upload()})
        self.assertContains(self.client.get(reverse('dashboard')), 'profile/photo/small')


class BackButtonAndSplashTests(BaseCase):
    def back_href(self, response):
        match = re.search(r'<a href="([^"]*)"\s+aria-label="Go back"', response.content.decode())
        return match.group(1) if match else None

    def test_home_has_no_back_button(self):
        self.assertIsNone(self.back_href(self.client.get(reverse('dashboard'))))

    def test_inner_pages_go_back_to_home(self):
        for name in ('stock', 'customers', 'debts', 'sales', 'add_sale', 'profile'):
            with self.subTest(page=name):
                self.assertEqual(self.back_href(self.client.get(reverse(name))), reverse('dashboard'))

    def test_edit_product_goes_back_to_stock(self):
        response = self.client.get(reverse('edit_product', args=[self.shirt.pk]))
        self.assertEqual(self.back_href(response), reverse('stock'))

    def test_invoice_goes_back_to_sales(self):
        self.sell([(self.cap, 1)])
        response = self.client.get(reverse('invoice', args=[Sale.objects.get().pk]))
        self.assertEqual(self.back_href(response), reverse('sales'))

    def test_signed_out_pages(self):
        self.client.logout()
        self.assertIsNone(self.back_href(self.client.get(reverse('login'))))
        self.assertEqual(self.back_href(self.client.get(reverse('register'))), reverse('login'))
        self.assertEqual(self.back_href(self.client.get(reverse('password_reset'))), reverse('login'))

    def test_loading_animation_is_on_every_page(self):
        self.assertContains(self.client.get(reverse('dashboard')), 'id="splash"')
        self.client.logout()
        self.assertContains(self.client.get(reverse('login')), 'id="splash"')


class PasswordResetTests(TestCase):
    NEW_PASSWORD = 'N3w!passw0rd'

    def setUp(self):
        self.seller = make_seller('08044444444')
        self.seller.email = 'Ada@Example.com'
        self.seller.username = 'Ada'
        self.seller.save()

    def request_reset(self, identifier):
        return self.client.post(reverse('password_reset'), {'email': identifier})

    def link_from_email(self):
        return re.search(r'/reset/[\w-]+/[\w-]+/', mail.outbox[0].body).group(0)

    def test_login_page_links_to_reset(self):
        self.assertContains(self.client.get(reverse('login')), 'Forgot password?')

    def test_reset_by_phone_number_emails_the_address_on_file(self):
        response = self.request_reset('08044444444')
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['Ada@Example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Reset your Seller Ops password')
        self.assertIn('/reset/', mail.outbox[0].body)

    def test_phone_formats_are_normalised(self):
        self.request_reset('+234 804 444 4444')
        self.assertEqual(len(mail.outbox), 1)

    def test_reset_by_email_ignores_case(self):
        self.request_reset('ada@example.COM')
        self.assertEqual(len(mail.outbox), 1)

    def test_unknown_account_sends_nothing_but_looks_the_same(self):
        response = self.request_reset('08099999999')
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_account_without_email_gets_nothing(self):
        make_seller('08055555555')
        response = self.request_reset('08055555555')
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_full_reset_flow(self):
        self.request_reset('08044444444')
        page = self.client.get(self.link_from_email(), follow=True)
        self.assertContains(page, 'Choose a new password')
        set_url = page.redirect_chain[-1][0]
        response = self.client.post(set_url, {'new_password1': self.NEW_PASSWORD, 'new_password2': self.NEW_PASSWORD})
        self.assertRedirects(response, reverse('password_reset_complete'))
        self.client.logout()
        self.assertTrue(self.client.login(username='08044444444', password=self.NEW_PASSWORD))
        self.client.logout()
        self.assertFalse(self.client.login(username='08044444444', password=PASSWORD))

    def test_mismatched_new_passwords_are_rejected(self):
        self.request_reset('08044444444')
        page = self.client.get(self.link_from_email(), follow=True)
        response = self.client.post(page.redirect_chain[-1][0],
                                    {'new_password1': self.NEW_PASSWORD, 'new_password2': 'Different!pass1'})
        self.assertEqual(response.status_code, 200)
        self.client.logout()
        self.assertTrue(self.client.login(username='08044444444', password=PASSWORD))

    def test_link_works_only_once(self):
        self.request_reset('08044444444')
        link = self.link_from_email()
        page = self.client.get(link, follow=True)
        self.client.post(page.redirect_chain[-1][0],
                         {'new_password1': self.NEW_PASSWORD, 'new_password2': self.NEW_PASSWORD})
        self.assertContains(self.client.get(link), 'This link has expired')

    def test_garbage_link_shows_expired_page(self):
        self.assertContains(self.client.get('/reset/abc/def-123/'), 'This link has expired')

    def test_mail_server_failure_does_not_crash(self):
        with mock.patch('django.core.mail.message.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            response = self.request_reset('08044444444')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'could not send')


class RegistrationEmailTests(TestCase):
    def register(self, email):
        return self.client.post(reverse('register'), {
            'username': 'Joy', 'business_name': '', 'phone_number': '08022222222', 'email': email,
            'password': PASSWORD, 'confirm_password': PASSWORD})

    def test_email_is_saved_when_given(self):
        self.register('joy@example.com')
        self.assertEqual(Seller.objects.get().email, 'joy@example.com')

    def test_email_is_optional(self):
        self.register('')
        self.assertEqual(Seller.objects.get().email, '')

    def test_invalid_email_is_rejected(self):
        self.register('nope')
        self.assertFalse(Seller.objects.exists())
