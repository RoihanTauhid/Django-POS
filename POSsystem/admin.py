from django.contrib import admin
from .models import AuditLog, Kategori, Produk, Transaksi, DetailTransaksi, RiwayatStok, Supplier, Pembelian, ReturTransaksi, StokOpname

# Register your models here.

admin.site.register(Kategori)
admin.site.register(Produk)
admin.site.register(Transaksi)
admin.site.register(DetailTransaksi)
admin.site.register(RiwayatStok)
admin.site.register(Supplier)
admin.site.register(Pembelian)
admin.site.register(AuditLog)
admin.site.register(ReturTransaksi)
admin.site.register(StokOpname)