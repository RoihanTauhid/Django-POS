from django.db import models

# Create your models here.
class Kategori(models.Model):
    nama = models.CharField(max_length=100)
    
    def __str__(self):
        return self.nama

class Produk(models.Model):
    kategori = models.ForeignKey(Kategori, on_delete=models.SET_NULL, null=True)
    kode_barcode = models.CharField(max_length=50, unique=True, help_text="Scan barcode di sini")
    nama_produk = models.CharField(max_length=200)
    harga_beli = models.DecimalField(max_digits=10, decimal_places=2)
    harga_jual = models.DecimalField(max_digits=10, decimal_places=2)
    stok_gudang = models.IntegerField(default=0)
    stok_minimum = models.IntegerField(default=5)
    gambar = models.ImageField(upload_to='produk_images/', null=True, blank=True)

    def __str__(self):
        return f"{self.kode_barcode} - {self.nama_produk}"

class Transaksi(models.Model):
    METODE_CHOICES = (('TUNAI', 'Tunai'), ('QRIS', 'QRIS'), ('TRANSFER', 'Transfer'), ('DEBIT', 'Debit'))
    STATUS_CHOICES = (('SELESAI', 'Selesai'), ('PENDING', 'Menunggu Pembayaran'), ('VOID', 'Void'))
    kode_transaksi = models.CharField(max_length=50, unique=True)
    tanggal = models.DateTimeField(auto_now_add=True)
    total_harga = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    uang_dibayar = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    kembalian = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    diskon = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    metode_pembayaran = models.CharField(max_length=20, choices=METODE_CHOICES, default='TUNAI')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='SELESAI')
    # Kolom untuk payment gateway (Midtrans)
    snap_token = models.CharField(max_length=255, blank=True, null=True)
    tipe_pembayaran_gateway = models.CharField(max_length=50, blank=True, null=True, help_text="Contoh: qris, gopay, bank_transfer")
    waktu_bayar = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.kode_transaksi} - {self.tanggal.strftime('%d/%m/%Y')}"

    @property
    def metode_tampil(self):
        """Label metode pembayaran + channel gateway (mis. 'QRIS (qris)')."""
        label = self.get_metode_pembayaran_display()
        if self.tipe_pembayaran_gateway:
            return f"{label} ({self.tipe_pembayaran_gateway})"
        return label

class DetailTransaksi(models.Model):
    transaksi = models.ForeignKey(Transaksi, on_delete=models.CASCADE, related_name='items')
    produk = models.ForeignKey(Produk, on_delete=models.PROTECT)
    jumlah = models.IntegerField(default=1)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    harga_modal = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.produk.nama_produk} x {self.jumlah}"

class RiwayatStok(models.Model):
    TIPE_CHOICES = (
        ('MASUK', 'Barang Masuk (Restock)'),
        ('KELUAR', 'Barang Keluar (Penyesuaian/Rusak)'),
    )
    produk = models.ForeignKey(Produk, on_delete=models.CASCADE)
    tipe = models.CharField(max_length=10, choices=TIPE_CHOICES)
    jumlah = models.IntegerField()
    tanggal = models.DateTimeField(auto_now_add=True)
    keterangan = models.TextField(blank=True, null=True, help_text="Contoh: Tambahan dari supplier A, atau Barang rusak")

    def __str__(self):
        return f"{self.produk.nama_produk} - {self.tipe} ({self.jumlah})"


class Supplier(models.Model):
    nama = models.CharField(max_length=150)
    telepon = models.CharField(max_length=30, blank=True)
    alamat = models.TextField(blank=True)

    def __str__(self):
        return self.nama


class Pembelian(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)
    produk = models.ForeignKey(Produk, on_delete=models.PROTECT)
    jumlah = models.PositiveIntegerField()
    harga_satuan = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    tanggal = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.produk.nama_produk} - {self.jumlah} unit"


class AuditLog(models.Model):
    pengguna = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    aksi = models.CharField(max_length=100)
    keterangan = models.TextField(blank=True)
    waktu = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-waktu',)

    def __str__(self):
        return f"{self.aksi} - {self.waktu:%Y-%m-%d %H:%M}"


class ReturTransaksi(models.Model):
    detail = models.ForeignKey(DetailTransaksi, on_delete=models.PROTECT)
    jumlah = models.PositiveIntegerField()
    alasan = models.TextField(blank=True)
    pengguna = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True)
    tanggal = models.DateTimeField(auto_now_add=True)


class StokOpname(models.Model):
    produk = models.ForeignKey(Produk, on_delete=models.PROTECT)
    stok_sistem = models.IntegerField()
    stok_fisik = models.IntegerField()
    selisih = models.IntegerField()
    keterangan = models.TextField(blank=True)
    pengguna = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True)
    tanggal = models.DateTimeField(auto_now_add=True)