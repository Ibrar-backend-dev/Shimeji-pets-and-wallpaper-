"""
Public API routes.

Named per type so each Android app calls its own path — an app cannot receive
another type's content by omitting a query parameter. All three map onto the
same view class with `feature_slug` bound.
"""

from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    # Bootstrap
    path("manifest", views.ManifestView.as_view(), name="manifest"),
    # Taxonomy
    path("categories", views.CategoryListView.as_view(), name="category-list"),
    path("subcategories", views.SubcategoryListView.as_view(), name="subcategory-list"),
    # Per-type feeds
    path("wallpapers", views.WallpaperListView.as_view(), name="wallpaper-list"),
    path("wallpapers/<uuid:pk>", views.WallpaperDetailView.as_view(), name="wallpaper-detail"),
    path("shimeji", views.ShimejiListView.as_view(), name="shimeji-list"),
    path("shimeji/<uuid:pk>", views.ShimejiDetailView.as_view(), name="shimeji-detail"),
    path("battery", views.BatteryListView.as_view(), name="battery-list"),
    path("battery/<uuid:pk>", views.BatteryDetailView.as_view(), name="battery-detail"),
    # Mixed feed across every type the key may read
    path("items", views.MixedItemListView.as_view(), name="item-list"),
    path("items/<uuid:pk>", views.MixedItemDetailView.as_view(), name="item-detail"),
    path("items/<uuid:pk>/related", views.RelatedItemsView.as_view(), name="item-related"),
]
