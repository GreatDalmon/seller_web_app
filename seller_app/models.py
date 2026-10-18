from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager

# ==========================================
# 1. AUTHENTICATION & SELLER MODEL
# ==========================================
class SellerManager(BaseUserManager):
    def create_user(self, phone_number, password=None):
        if not phone_number:
            raise ValueError("Sellers must have a phone number")
        user = self.model(phone_number=phone_number)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None):
        user = self.create_user(phone_number, password=password)
        user.is_admin = True
        user.save(using=self._db)
        return user

class Seller(AbstractBaseUser):
    """Custom User model prioritizing phone numbers for login."""
    phone_number = models.CharField(max_length=15, unique=True)
    username = models.CharField(max_length=50, blank=True)
    business_name = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    is_admin = models.BooleanField(default=False)

    objects = SellerManager()

    USERNAME_FIELD = 'phone_number'

    def __str__(self):
        return self.username or self.phone_number

    # Required for Django Admin access
    @property
    def is_staff(self):
        return self.is_admin

    def has_perm(self, perm, obj=None):
        return True

    def has_module_perms(self, app_label):
        return True

# ==========================================
# 2. PRODUCTS & STOCK
# ==========================================
class Product(models.Model):
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(max_length=100)
    stock = models.IntegerField(default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    LOW_STOCK_LEVEL = 5

    @property
    def is_low(self):
        return self.stock <= self.LOW_STOCK_LEVEL

    def __str__(self):
        return f"{self.name} ({self.stock} in stock)"

# ==========================================
# 3. CUSTOMERS & DEBT
# ==========================================
class Customer(models.Model):
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name='customers')
    name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15, blank=True)

    @property
    def debt_balance(self):
        # Dynamically calculates how much this customer owes across all sales
        return sum(sale.balance_due for sale in self.sales.all())

    def __str__(self):
        return self.name

# ==========================================
# 4. SALES & TRACKING
# ==========================================
class Sale(models.Model):
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name='sales')
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales')
    date = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    @property
    def balance_due(self):
        return self.total_amount - self.amount_paid

    @property
    def is_fully_paid(self):
        return self.amount_paid >= self.total_amount

    def __str__(self):
        return f"Sale {self.id} on {self.date.strftime('%Y-%m-%d')}"

class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.IntegerField(default=1)
    price_at_sale = models.DecimalField(max_digits=10, decimal_places=2)

    def save(self, *args, **kwargs):
        # Automatically deduct stock when a new sale item is created
        if not self.pk:
            self.product.stock -= self.quantity
            self.product.save(update_fields=['stock'])
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity} x {self.product.name}"