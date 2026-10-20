from django.urls import path
from . import views, pwa

urlpatterns = [
    path('manifest.webmanifest', pwa.manifest, name='manifest'),
    path('sw.js', pwa.service_worker, name='service_worker'),
    path('offline/', pwa.offline, name='offline'),
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),
    path('', views.dashboard, name='dashboard'),
    path('stock/', views.stock_list, name='stock'),
    path('stock/add/', views.add_product, name='add_product'),
    path('stock/<int:pk>/update/', views.update_stock, name='update_stock'),
    path('sell/', views.add_sale, name='add_sale'),
    path('customers/', views.customers_list, name='customers'),
    path('customers/add/', views.add_customer, name='add_customer'),
    path('customers/<int:pk>/update/', views.update_customer, name='update_customer'),
    path('debts/', views.debts_list, name='debts'),
    path('debts/<int:pk>/pay/', views.record_payment, name='record_payment'),
]
