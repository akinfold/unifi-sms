from django.urls import path
from django.contrib.auth.views import LogoutView
from inbox import connection_views, smtp_views, views

urlpatterns = [
    path("", views.home, name="home"),
    path("settings/device/", connection_views.device_settings, name="device-settings"),
    path("api/device/discover/", connection_views.device_discovery, name="device-discovery"),
    path("settings/smtp/", smtp_views.smtp_settings, name="smtp-settings"),
    path("setup/", views.setup, name="setup"),
    path("login/", views.AdminLogin.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("api/messages/", views.message_list, name="message-list"),
    path("api/messages/<int:pk>/", views.message_detail, name="message-detail"),
    path("api/delete/", views.delete_messages, name="delete-messages"),
    path("api/phone/refresh/", views.refresh_phone_number, name="refresh-phone-number"),
    path("api/status/", views.status, name="status"),
    path("healthz/", views.health, name="health"),
]
