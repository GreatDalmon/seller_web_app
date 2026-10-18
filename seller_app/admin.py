from django.contrib import admin
from .models import Seller, Product, Customer, Sale, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0


@admin.register(Seller)
class SellerAdmin(admin.ModelAdmin):
    list_display = ('phone_number', 'username', 'business_name', 'is_active', 'is_admin')
    search_fields = ('phone_number', 'username', 'business_name')
    exclude = ('password',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'seller', 'price', 'stock')
    list_filter = ('seller',)
    search_fields = ('name',)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone_number', 'seller')
    search_fields = ('name', 'phone_number')


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ('id', 'seller', 'customer', 'date', 'total_amount', 'amount_paid')
    list_filter = ('seller',)
    inlines = [SaleItemInline]
