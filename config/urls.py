from django.urls import path
from django.contrib.auth.views import LogoutView
from inbox import api_views, connection_views, integration_views, mcp_views, outbound_views, smtp_views, views

urlpatterns = [
    path("", views.home, name="home"),
    path("send/", outbound_views.send_sms, name="send-sms"),
    path("send/status/", outbound_views.status, name="outbound-status"),
    path("send/action/", outbound_views.action, name="outbound-action"),
    path("settings/device/", connection_views.device_settings, name="device-settings"),
    path("api/device/discover/", connection_views.device_discovery, name="device-discovery"),
    path("settings/smtp/", smtp_views.smtp_settings, name="smtp-settings"),
    path("settings/integrations/", integration_views.integrations, name="integration-settings"),
    path("settings/webhooks/", integration_views.webhooks, name="webhook-settings"),
    path("api/integrations/key/", integration_views.generate_key, name="generate-integration-key"),
    path("api/v1/messages/", api_views.messages, name="rest-messages"),
    path("api/v1/messages/<int:pk>/", api_views.message, name="rest-message"),
    path("api/v1/outbound/", api_views.outbound_collection, name="rest-outbound"),
    path("api/v1/outbound/<uuid:pk>/", api_views.outbound_message, name="rest-outbound-message"),
    path("mcp/", mcp_views.endpoint, name="mcp"),
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
