from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health),
    path("config/", views.config),
    path("orders/", views.list_orders),
    path("orders/<int:order_id>/", views.get_order),
    path("orders/<int:order_id>/fields/", views.edit_fields),
    path("orders/<int:order_id>/approve/", views.approve_order),
    path("orders/<int:order_id>/reject/", views.reject_order),
    path("orders/<int:order_id>/audit/", views.order_audit),
    path("orders/<int:order_id>/reprocess/", views.reprocess_order_view),
    path("orders/<int:order_id>/export/", views.export_order_view),
    path("poll/", views.trigger_poll),
]
