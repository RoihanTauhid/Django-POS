import hashlib
import hmac
import uuid
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import midtransclient

from django.db import transaction
from django.db import IntegrityError
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import FileResponse, HttpResponse, JsonResponse
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek, TruncYear
from django.shortcuts import get_object_or_404, redirect, render
import csv
from django.core.paginator import Paginator
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from .decorators import group_required

from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import AuditLog, DetailTransaksi, Kategori, Pembelian, Produk, ReturTransaksi, RiwayatStok, StokOpname, Supplier, Transaksi


class StokTidakCukupError(Exception):
    """Dilempar saat stok produk berubah dan tidak mencukupi saat checkout."""


# Pemetaan metode pembayaran POS -> channel pembayaran Midtrans
MIDTRANS_ENABLED_PAYMENTS = {
    'QRIS': ['qris'],
    'TRANSFER': ['bank_transfer', 'echannel'],
    'DEBIT': ['credit_card'],
}


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard_redirect')
    if request.method == 'POST':
        user = authenticate(request, username=request.POST.get('username'), password=request.POST.get('password'))
        if user:
            login(request, user)
            return redirect(request.GET.get('next', 'dashboard_redirect'))
        messages.error(request, 'Username atau password tidak valid.')
    return render(request, 'POSsystem/login.html')


def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def dashboard_redirect(request):
    if request.user.groups.filter(name__in=['Kasir', 'Supervisor']).exists():
        return redirect('halaman_kasir')
    if request.user.groups.filter(name='Admin Gudang').exists():
        return redirect('halaman_gudang')
    if request.user.groups.filter(name='Owner').exists():
        return redirect('halaman_analytics')
    if request.user.is_superuser:
        return redirect('/admin/')
    logout(request)
    return redirect('login')


@login_required
@group_required('Owner')
def backup_database(request):
    if not request.user.is_superuser:
        return HttpResponse('Forbidden', status=403)
    database_path = settings.DATABASES['default']['NAME']
    return FileResponse(open(database_path, 'rb'), as_attachment=True, filename='ecomora-backup.sqlite3')


@login_required
@group_required('Kasir', 'Supervisor')
def halaman_kasir(request):
    keranjang = request.session.get('keranjang', {})
    query = request.GET.get('barcode', '').strip()
    kategori_aktif = request.GET.get('kategori')
    semua_kategori = Kategori.objects.all()

    if query:
        semua_produk = Produk.objects.filter(
            Q(kode_barcode__icontains=query) | Q(nama_produk__icontains=query)
        )
        produk_exact = Produk.objects.filter(kode_barcode=query).first()
        if produk_exact:
            tambah_item_ke_session(keranjang, produk_exact)
            request.session['keranjang'] = keranjang
            return redirect('halaman_kasir')
        if query:
            messages.error(request, f'Barcode atau produk "{query}" tidak ditemukan.')
    elif kategori_aktif:
        semua_produk = Produk.objects.filter(kategori__id=kategori_aktif)
    else:
        semua_produk = Produk.objects.all()

    total_belanja = sum(item['subtotal'] for item in keranjang.values())
    context = {
        'daftar_produk': semua_produk,
        'semua_kategori': semua_kategori,
        'kategori_aktif': str(kategori_aktif),
        'keranjang': keranjang,
        'total_belanja': total_belanja,
        'total_item': sum(item['jumlah'] for item in keranjang.values()),
    }
    return render(request, 'POSsystem/index.html', context)


def tambah_item_ke_session(keranjang, produk):
    produk_id = str(produk.id)
    if produk_id in keranjang:
        keranjang[produk_id]['jumlah'] += 1
        keranjang[produk_id]['subtotal'] = keranjang[produk_id]['jumlah'] * float(produk.harga_jual)
    else:
        keranjang[produk_id] = {
            'id': produk.id,
            'nama': produk.nama_produk,
            'harga': float(produk.harga_jual),
            'jumlah': 1,
            'subtotal': float(produk.harga_jual),
        }


@login_required
@group_required('Kasir', 'Supervisor')
def tambah_ke_keranjang(request, produk_id):
    keranjang = request.session.get('keranjang', {})
    produk = get_object_or_404(Produk, id=produk_id)
    if produk.stok_gudang < keranjang.get(str(produk.id), {}).get('jumlah', 0) + 1:
        messages.error(request, f'Stok {produk.nama_produk} tidak mencukupi.')
        return redirect('halaman_kasir')
    tambah_item_ke_session(keranjang, produk)
    request.session['keranjang'] = keranjang
    return redirect('halaman_kasir')


@login_required
@group_required('Kasir', 'Supervisor')
def ubah_keranjang(request, produk_id, aksi):
    keranjang = request.session.get('keranjang', {})
    produk_key = str(produk_id)
    if produk_key not in keranjang:
        return redirect('halaman_kasir')
    if aksi == 'tambah':
        produk = get_object_or_404(Produk, id=produk_id)
        if keranjang[produk_key]['jumlah'] >= produk.stok_gudang:
            messages.error(request, f'Stok {produk.nama_produk} tidak mencukupi.')
            return redirect('halaman_kasir')
        keranjang[produk_key]['jumlah'] += 1
    elif aksi == 'kurang':
        keranjang[produk_key]['jumlah'] -= 1
        if keranjang[produk_key]['jumlah'] <= 0:
            del keranjang[produk_key]
            request.session['keranjang'] = keranjang
            return redirect('halaman_kasir')
    elif aksi == 'hapus':
        del keranjang[produk_key]
        request.session['keranjang'] = keranjang
        return redirect('halaman_kasir')
    keranjang[produk_key]['subtotal'] = keranjang[produk_key]['jumlah'] * keranjang[produk_key]['harga']
    request.session['keranjang'] = keranjang
    return redirect('halaman_kasir')


@login_required
@group_required('Kasir', 'Supervisor')
def bersihkan_keranjang(request):
    request.session.pop('keranjang', None)
    return redirect('halaman_kasir')


@login_required
@group_required('Kasir', 'Supervisor')
def checkout(request):
    if request.method != 'POST':
        return redirect('halaman_kasir')

    keranjang = request.session.get('keranjang', {})
    if not keranjang:
        return redirect('halaman_kasir')

    metode = request.POST.get('metode_pembayaran', 'TUNAI')

    total_sebelum_diskon = Decimal(str(sum(item['subtotal'] for item in keranjang.values())))
    try:
        diskon = max(Decimal('0'), Decimal(request.POST.get('diskon', '0')))
    except InvalidOperation:
        diskon = Decimal('0')
    diskon = min(diskon, total_sebelum_diskon)
    total_belanja = total_sebelum_diskon - diskon

    uang_dibayar = Decimal('0')
    if metode == 'TUNAI':
        try:
            uang_dibayar = Decimal(request.POST.get('uang_dibayar', '0'))
        except InvalidOperation:
            uang_dibayar = Decimal('0')
        if uang_dibayar < total_belanja:
            messages.error(request, 'Uang yang diberikan masih kurang dari total belanja.')
            return redirect('halaman_kasir')
    elif metode not in MIDTRANS_ENABLED_PAYMENTS:
        messages.error(request, 'Metode pembayaran tidak valid.')
        return redirect('halaman_kasir')

    # Stok langsung direservasi saat checkout; jika pembayaran online
    # gagal/dibatalkan/kedaluwarsa, stok dikembalikan otomatis lewat webhook.
    try:
        with transaction.atomic():
            transaksi_baru = Transaksi.objects.create(
                kode_transaksi=f"TRX-{uuid.uuid4().hex[:8].upper()}",
                total_harga=total_belanja,
                uang_dibayar=uang_dibayar,
                kembalian=(uang_dibayar - total_belanja) if metode == 'TUNAI' else Decimal('0'),
                diskon=diskon,
                metode_pembayaran=metode,
                status='SELESAI' if metode == 'TUNAI' else 'PENDING',
            )
            for produk_id, data in keranjang.items():
                produk = Produk.objects.select_for_update().get(id=produk_id)
                if produk.stok_gudang < data['jumlah']:
                    raise StokTidakCukupError(produk.nama_produk)
                DetailTransaksi.objects.create(
                    transaksi=transaksi_baru,
                    produk=produk,
                    jumlah=data['jumlah'],
                    subtotal=data['subtotal'],
                    harga_modal=produk.harga_beli,
                )
                produk.stok_gudang -= data['jumlah']
                produk.save(update_fields=['stok_gudang'])
    except StokTidakCukupError as nama_produk:
        messages.error(request, f'Stok {nama_produk} berubah dan tidak mencukupi.')
        return redirect('halaman_kasir')

    del request.session['keranjang']

    if metode == 'TUNAI':
        AuditLog.objects.create(pengguna=request.user, aksi='CHECKOUT', keterangan=transaksi_baru.kode_transaksi)
        return redirect('cetak_struk', transaksi_id=transaksi_baru.id)

    # ===== Pembayaran online via Midtrans Snap =====
    try:
        transaksi_baru.snap_token = buat_snap_token(transaksi_baru)
        transaksi_baru.save(update_fields=['snap_token'])
    except Exception as exc:
        batalkan_transaksi_gateway(transaksi_baru.id)
        messages.error(request, f'Gagal membuat tagihan Midtrans: {exc}')
        return redirect('halaman_kasir')

    AuditLog.objects.create(pengguna=request.user, aksi='CHECKOUT_ONLINE', keterangan=f"{transaksi_baru.kode_transaksi} ({metode})")
    return redirect('halaman_pembayaran', transaksi_id=transaksi_baru.id)


def buat_snap_token(transaksi):
    """Membuat Snap token Midtrans untuk sebuah transaksi."""
    snap = midtransclient.Snap(
        is_production=settings.MIDTRANS_IS_PRODUCTION,
        server_key=settings.MIDTRANS_SERVER_KEY,
    )
    item_details = []
    for detail in transaksi.items.select_related('produk'):
        harga_satuan = float(detail.subtotal) / detail.jumlah if detail.jumlah else float(detail.subtotal)
        item_details.append({
            'id': str(detail.produk_id),
            'price': round(harga_satuan, 2),
            'quantity': detail.jumlah,
            'name': detail.produk.nama_produk[:50],
        })
    if transaksi.diskon > 0:
        item_details.append({'id': 'DISKON', 'price': -float(transaksi.diskon), 'quantity': 1, 'name': 'Diskon'})

    parameter = {
        'transaction_details': {
            'order_id': transaksi.kode_transaksi,
            'gross_amount': float(transaksi.total_harga),
        },
        'item_details': item_details,
        'customer_details': {'first_name': 'Pelanggan Kasir'},
        'expiry': {'unit': 'hour', 'duration': 24},
    }
    # Secara default popup menampilkan SEMUA channel yang aktif di akun Midtrans,
    # agar popup tidak kosong jika suatu channel (mis. QRIS) belum diaktifkan.
    # Set MIDTRANS_RESTRICT_CHANNELS=True untuk membatasi sesuai pilihan kasir.
    if getattr(settings, 'MIDTRANS_RESTRICT_CHANNELS', False):
        parameter['enabled_payments'] = MIDTRANS_ENABLED_PAYMENTS.get(transaksi.metode_pembayaran, [])
    response = snap.create_transaction(parameter)
    return response['token']


def selesaikan_transaksi_gateway(transaksi_id, tipe_pembayaran=None):
    """Menandai transaksi online sebagai SELESAI (dipanggil dari webhook)."""
    with transaction.atomic():
        transaksi = Transaksi.objects.select_for_update().filter(id=transaksi_id).first()
        if not transaksi or transaksi.status == 'SELESAI':
            return
        transaksi.status = 'SELESAI'
        transaksi.uang_dibayar = transaksi.total_harga
        transaksi.kembalian = Decimal('0')
        transaksi.tipe_pembayaran_gateway = tipe_pembayaran or ''
        transaksi.waktu_bayar = timezone.now()
        transaksi.save(update_fields=['status', 'uang_dibayar', 'kembalian', 'tipe_pembayaran_gateway', 'waktu_bayar'])
        AuditLog.objects.create(
            aksi='PEMBAYARAN_GATEWAY',
            keterangan=f"{transaksi.kode_transaksi} lunas via {tipe_pembayaran or transaksi.metode_pembayaran}",
        )


def batalkan_transaksi_gateway(transaksi_id):
    """Membatalkan transaksi PENDING dan mengembalikan stok yang direservasi."""
    with transaction.atomic():
        transaksi = Transaksi.objects.select_for_update().filter(id=transaksi_id).first()
        if not transaksi or transaksi.status != 'PENDING':
            return
        for detail in transaksi.items.select_related('produk'):
            produk = Produk.objects.select_for_update().get(id=detail.produk_id)
            produk.stok_gudang += detail.jumlah
            produk.save(update_fields=['stok_gudang'])
        transaksi.status = 'VOID'
        transaksi.save(update_fields=['status'])
        AuditLog.objects.create(aksi='PEMBAYARAN_DIBATALKAN', keterangan=transaksi.kode_transaksi)


@login_required
@group_required('Kasir', 'Supervisor')
def halaman_pembayaran(request, transaksi_id):
    """Halaman pembayaran online dengan popup Midtrans Snap."""
    transaksi = get_object_or_404(Transaksi, id=transaksi_id)
    if transaksi.status == 'SELESAI':
        return redirect('cetak_struk', transaksi_id=transaksi.id)
    if transaksi.status != 'PENDING':
        messages.error(request, 'Transaksi ini sudah tidak dapat dibayar.')
        return redirect('halaman_kasir')
    return render(request, 'POSsystem/pembayaran.html', {
        'transaksi': transaksi,
        'detail_transaksi': transaksi.items.select_related('produk'),
        'midtrans_client_key': settings.MIDTRANS_CLIENT_KEY,
        'midtrans_snap_url': (
            'https://app.midtrans.com/snap/snap.js'
            if settings.MIDTRANS_IS_PRODUCTION
            else 'https://app.sandbox.midtrans.com/snap/snap.js'
        ),
    })


@login_required
@group_required('Kasir', 'Supervisor')
def status_pembayaran(request, transaksi_id):
    """Endpoint JSON untuk polling status pembayaran dari halaman bayar."""
    transaksi = get_object_or_404(Transaksi, id=transaksi_id)
    return JsonResponse({'status': transaksi.status})


@login_required
@group_required('Kasir', 'Supervisor')
def batalkan_pembayaran(request, transaksi_id):
    """Kasir membatalkan pembayaran online yang belum selesai."""
    if request.method == 'POST':
        batalkan_transaksi_gateway(transaksi_id)
        messages.info(request, 'Pembayaran dibatalkan dan stok telah dikembalikan.')
        return redirect('halaman_kasir')
    return redirect('halaman_pembayaran', transaksi_id=transaksi_id)


@csrf_exempt
def midtrans_notification(request):
    """Webhook HTTP Notification dari Midtrans (dipanggil server Midtrans, tanpa login).

    Daftarkan URL ini di dashboard Midtrans:
    Settings -> Configuration -> Payment Notification URL
    Contoh: https://domain-anda.com/payment/midtrans/notifikasi/
    """
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        notif = json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=400)

    # Verifikasi signature: sha512(order_id + status_code + gross_amount + server_key)
    order_id = str(notif.get('order_id', ''))
    status_code = str(notif.get('status_code', ''))
    gross_amount = str(notif.get('gross_amount', ''))
    signature_expected = hashlib.sha512(
        f"{order_id}{status_code}{gross_amount}{settings.MIDTRANS_SERVER_KEY}".encode()
    ).hexdigest()
    if not hmac.compare_digest(signature_expected, str(notif.get('signature_key', ''))):
        return HttpResponse('Signature tidak valid', status=403)

    transaksi = Transaksi.objects.filter(kode_transaksi=order_id).first()
    if not transaksi:
        return HttpResponse('Transaksi tidak ditemukan', status=404)

    status_midtrans = notif.get('transaction_status', '')
    fraud_status = notif.get('fraud_status', '')
    tipe_pembayaran = notif.get('payment_type', '')

    if status_midtrans == 'capture' and fraud_status == 'challenge':
        pass  # Menunggu review fraud, tetap PENDING
    elif status_midtrans in ('capture', 'settlement'):
        selesaikan_transaksi_gateway(transaksi.id, tipe_pembayaran)
    elif status_midtrans in ('deny', 'cancel', 'expire'):
        batalkan_transaksi_gateway(transaksi.id)

    return HttpResponse('OK')


@login_required
@group_required('Kasir', 'Supervisor', 'Owner')
def cetak_struk(request, transaksi_id):
    transaksi = get_object_or_404(Transaksi, id=transaksi_id)
    return render(request, 'POSsystem/struk.html', {
        'transaksi': transaksi,
        'detail_transaksi': DetailTransaksi.objects.filter(transaksi=transaksi),
    })


@login_required
@group_required('Admin Gudang', 'Supervisor')
def halaman_gudang(request):
    if request.method == 'POST':
        kode_barcode = request.POST.get('kode_barcode', '').strip()
        produk_id = request.POST.get('produk_id')
        tipe = request.POST.get('tipe')
        try:
            jumlah = int(request.POST.get('jumlah', 0))
        except (TypeError, ValueError):
            jumlah = 0
        if kode_barcode:
            produk_id = Produk.objects.filter(kode_barcode=kode_barcode).values_list('id', flat=True).first()
            if not produk_id:
                messages.error(request, 'Barcode belum terdaftar. Tambahkan produk terlebih dahulu.')
                return redirect('tambah_produk_baru')
        if jumlah > 0 and tipe in {'MASUK', 'KELUAR'}:
            produk = get_object_or_404(Produk, id=produk_id)
            if tipe == 'MASUK':
                produk.stok_gudang += jumlah
            elif produk.stok_gudang >= jumlah:
                produk.stok_gudang -= jumlah
            else:
                return redirect('halaman_gudang')
            produk.save(update_fields=['stok_gudang'])
            RiwayatStok.objects.create(produk=produk, tipe=tipe, jumlah=jumlah, keterangan=request.POST.get('keterangan', ''))
            AuditLog.objects.create(pengguna=request.user, aksi='MUTASI_STOK', keterangan=f'{produk.nama_produk}: {tipe} {jumlah}')
            return redirect('halaman_gudang')

    return render(request, 'POSsystem/gudang.html', {
        'daftar_produk': Produk.objects.select_related('kategori').all(),
        'riwayat_terbaru': RiwayatStok.objects.select_related('produk').order_by('-tanggal')[:10],
    })


@login_required
@group_required('Admin Gudang', 'Supervisor')
def halaman_stok(request):
    query = request.GET.get('q', '').strip()
    produk = Produk.objects.select_related('kategori').all()
    if query:
        produk = produk.filter(Q(nama_produk__icontains=query) | Q(kode_barcode__icontains=query))
    return render(request, 'POSsystem/stok.html', {
        'daftar_produk': produk,
        'query': query,
    })


@login_required
@group_required('Admin Gudang', 'Supervisor')
def tambah_produk_baru(request):
    if request.method == 'POST':
        kategori_id = request.POST.get('kategori_id')
        Produk.objects.create(
            nama_produk=request.POST.get('nama_produk'),
            kode_barcode=request.POST.get('kode_barcode'),
            kategori=Kategori.objects.filter(id=kategori_id).first() if kategori_id else None,
            harga_beli=request.POST.get('harga_beli'),
            harga_jual=request.POST.get('harga_jual'),
            stok_gudang=request.POST.get('stok_gudang'),
            stok_minimum=request.POST.get('stok_minimum') or 5,
            gambar=request.FILES.get('gambar'),
        )
        return redirect('halaman_stok')
    return render(request, 'POSsystem/tambah_produk.html', {'semua_kategori': Kategori.objects.all()})


@login_required
@group_required('Admin Gudang', 'Supervisor')
def edit_produk(request, produk_id):
    produk = get_object_or_404(Produk, id=produk_id)
    if request.method == 'POST':
        produk.nama_produk = request.POST.get('nama_produk')
        produk.kode_barcode = request.POST.get('kode_barcode')
        produk.kategori = Kategori.objects.filter(id=request.POST.get('kategori_id')).first() if request.POST.get('kategori_id') else None
        produk.harga_beli = request.POST.get('harga_beli')
        produk.harga_jual = request.POST.get('harga_jual')
        produk.stok_gudang = request.POST.get('stok_gudang')
        produk.stok_minimum = request.POST.get('stok_minimum') or 5
        if request.FILES.get('gambar'):
            produk.gambar = request.FILES['gambar']
        produk.save()
        return redirect('halaman_stok')
    return render(request, 'POSsystem/tambah_produk.html', {'semua_kategori': Kategori.objects.all(), 'produk_edit': produk})


@login_required
@group_required('Admin Gudang', 'Supervisor')
def hapus_produk(request, produk_id):
    if request.method == 'POST':
        try:
            get_object_or_404(Produk, id=produk_id).delete()
        except IntegrityError:
            messages.error(request, 'Produk yang sudah masuk transaksi tidak dapat dihapus.')
    return redirect('halaman_stok')


@login_required
@group_required('Admin Gudang', 'Supervisor')
def tambah_kategori(request):
    if request.method == 'POST':
        nama = request.POST.get('nama', '').strip()
        if nama:
            Kategori.objects.get_or_create(nama=nama)
        return redirect('tambah_produk_baru')
    return render(request, 'POSsystem/tambah_kategori.html')


@login_required
@group_required('Admin Gudang', 'Supervisor')
def halaman_pembelian(request):
    if request.method == 'POST':
        produk = get_object_or_404(Produk, id=request.POST.get('produk_id'))
        jumlah = int(request.POST.get('jumlah', 0))
        harga_satuan = Decimal(request.POST.get('harga_satuan', '0'))
        if jumlah > 0 and harga_satuan >= 0:
            supplier_id = request.POST.get('supplier_id')
            with transaction.atomic():
                produk.stok_gudang += jumlah
                produk.save(update_fields=['stok_gudang'])
                Pembelian.objects.create(supplier_id=supplier_id or None, produk=produk, jumlah=jumlah, harga_satuan=harga_satuan, total=harga_satuan * jumlah)
                RiwayatStok.objects.create(produk=produk, tipe='MASUK', jumlah=jumlah, keterangan='Pembelian supplier')
                AuditLog.objects.create(pengguna=request.user, aksi='PEMBELIAN', keterangan=f'{produk.nama_produk}: {jumlah} unit')
            return redirect('halaman_pembelian')
    return render(request, 'POSsystem/pembelian.html', {
        'daftar_produk': Produk.objects.all(),
        'daftar_supplier': Supplier.objects.all(),
        'pembelian_terbaru': Pembelian.objects.select_related('produk', 'supplier').order_by('-tanggal')[:20],
    })


@login_required
@group_required('Admin Gudang', 'Supervisor')
def tambah_supplier(request):
    if request.method == 'POST':
        nama = request.POST.get('nama', '').strip()
        if nama:
            Supplier.objects.create(nama=nama, telepon=request.POST.get('telepon', ''), alamat=request.POST.get('alamat', ''))
        return redirect('halaman_pembelian')
    return render(request, 'POSsystem/tambah_supplier.html')


@login_required
@group_required('Owner', 'Supervisor')
def halaman_analytics(request):
    hari_ini = timezone.localdate()
    awal_minggu = hari_ini - timedelta(days=hari_ini.weekday())
    awal_bulan = hari_ini.replace(day=1)
    awal_tahun = hari_ini.replace(month=1, day=1)

    def hitung_keuangan(queryset):
        pendapatan = queryset.aggregate(total=Sum('total_harga'))['total'] or 0
        keuntungan = DetailTransaksi.objects.filter(transaksi__in=queryset).aggregate(
            total=Sum(ExpressionWrapper((F('produk__harga_jual') - F('harga_modal')) * F('jumlah'), output_field=DecimalField(max_digits=12, decimal_places=2)))
        )['total'] or 0
        return {
            'pendapatan': pendapatan,
            'keuntungan': keuntungan,
            'jumlah': queryset.count()
        }

    transaksi_semua = Transaksi.objects.all()
    trx_hari_ini = transaksi_semua.filter(tanggal__date=hari_ini).order_by('-tanggal')
    
    keuangan = {
        'hari_ini': hitung_keuangan(trx_hari_ini),
        'minggu_ini': hitung_keuangan(transaksi_semua.filter(tanggal__date__gte=awal_minggu)),
        'bulan_ini': hitung_keuangan(transaksi_semua.filter(tanggal__date__gte=awal_bulan)),
        'tahun_ini': hitung_keuangan(transaksi_semua.filter(tanggal__date__gte=awal_tahun)),
    }
    penjualan_hari_ini = list(DetailTransaksi.objects.filter(
        transaksi__tanggal__date=hari_ini
    ).values('produk__nama_produk').annotate(total=Sum('jumlah')).order_by('-total'))

    awal_periode = hari_ini - timedelta(days=6)
    penjualan_per_hari = DetailTransaksi.objects.filter(
        transaksi__tanggal__date__range=(awal_periode, hari_ini)
    ).values('produk__nama_produk', 'transaksi__tanggal__date').annotate(total=Sum('jumlah'))
    pola_produk = defaultdict(list)
    for baris in penjualan_per_hari:
        pola_produk[baris['produk__nama_produk']].append(baris['total'])
    produk_stabil = sorted(
        (
            {'nama': nama, 'total': sum(jumlah), 'hari_aktif': len(jumlah)}
            for nama, jumlah in pola_produk.items()
            if len(jumlah) >= 3
        ),
        key=lambda produk: (-produk['hari_aktif'], -produk['total'], produk['nama']),
    )[:5]

    produk_terbanyak = penjualan_hari_ini[:5]
    produk_tersedikit = sorted(penjualan_hari_ini, key=lambda produk: (produk['total'], produk['produk__nama_produk']))[:5]
    return render(request, 'POSsystem/analytics.html', {
        'semua_transaksi': trx_hari_ini,
        'keuangan': keuangan,
        'hari_ini': hari_ini,
        'produk_terbanyak': produk_terbanyak,
        'produk_tersedikit': produk_tersedikit,
        'produk_stabil': produk_stabil,
        'produk_terbanyak_json': json.dumps(produk_terbanyak),
        'produk_tersedikit_json': json.dumps(produk_tersedikit),
        'produk_stabil_json': json.dumps(produk_stabil),
    })


@login_required
@group_required('Owner')
def export_transaksi(request):
    tanggal_mulai = request.GET.get('mulai')
    tanggal_selesai = request.GET.get('selesai')
    transaksi = Transaksi.objects.all().order_by('tanggal')
    if tanggal_mulai:
        transaksi = transaksi.filter(tanggal__date__gte=tanggal_mulai)
    if tanggal_selesai:
        transaksi = transaksi.filter(tanggal__date__lte=tanggal_selesai)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="laporan-transaksi.csv"'
    response.write('\ufeffTanggal,Kode Transaksi,Total,Uang Dibayar,Kembalian\n')
    for item in transaksi:
        response.write(f'{item.tanggal:%Y-%m-%d %H:%M},{item.kode_transaksi},{item.total_harga},{item.uang_dibayar},{item.kembalian}\n')
    return response


@login_required
@group_required('Supervisor')
def void_transaksi(request, transaksi_id):
    if request.method == 'POST':
        with transaction.atomic():
            transaksi_obj = get_object_or_404(Transaksi.objects.select_for_update(), id=transaksi_id)
            if transaksi_obj.status == 'SELESAI':
                for detail in transaksi_obj.items.select_related('produk'):
                    produk = Produk.objects.select_for_update().get(id=detail.produk_id)
                    produk.stok_gudang += detail.jumlah
                    produk.save(update_fields=['stok_gudang'])
                transaksi_obj.status = 'VOID'
                transaksi_obj.save(update_fields=['status'])
                AuditLog.objects.create(pengguna=request.user, aksi='VOID_TRANSAKSI', keterangan=transaksi_obj.kode_transaksi)
    return redirect('halaman_riwayat_transaksi')


@login_required
@group_required('Supervisor')
def retur_transaksi(request, detail_id):
    detail = get_object_or_404(DetailTransaksi, id=detail_id)
    if request.method == 'POST':
        jumlah = int(request.POST.get('jumlah', 0))
        sudah_diretur = ReturTransaksi.objects.filter(detail=detail).aggregate(total=Sum('jumlah'))['total'] or 0
        if 0 < jumlah <= detail.jumlah - sudah_diretur:
            with transaction.atomic():
                produk = Produk.objects.select_for_update().get(id=detail.produk_id)
                produk.stok_gudang += jumlah
                produk.save(update_fields=['stok_gudang'])
                ReturTransaksi.objects.create(detail=detail, jumlah=jumlah, alasan=request.POST.get('alasan', ''), pengguna=request.user)
                AuditLog.objects.create(pengguna=request.user, aksi='RETUR_BARANG', keterangan=f'{detail.produk.nama_produk}: {jumlah}')
    return redirect('cetak_struk', transaksi_id=detail.transaksi_id)


@login_required
@group_required('Admin Gudang', 'Supervisor')
def halaman_opname(request):
    if request.method == 'POST':
        produk = get_object_or_404(Produk, id=request.POST.get('produk_id'))
        stok_fisik = int(request.POST.get('stok_fisik', 0))
        selisih = stok_fisik - produk.stok_gudang
        if stok_fisik >= 0:
            produk.stok_gudang = stok_fisik
            produk.save(update_fields=['stok_gudang'])
            StokOpname.objects.create(produk=produk, stok_sistem=stok_fisik - selisih, stok_fisik=stok_fisik, selisih=selisih, keterangan=request.POST.get('keterangan', ''), pengguna=request.user)
            AuditLog.objects.create(pengguna=request.user, aksi='STOK_OPNAME', keterangan=f'{produk.nama_produk}: selisih {selisih}')
            return redirect('halaman_opname')
    return render(request, 'POSsystem/opname.html', {'daftar_produk': Produk.objects.all(), 'opname_terbaru': StokOpname.objects.select_related('produk').order_by('-tanggal')[:20]})


@login_required
@group_required('Owner', 'Supervisor')
def halaman_audit(request):
    from django.contrib.auth.models import User
    query = request.GET.get('q', '')
    pengguna_id = request.GET.get('pengguna', '')
    export = request.GET.get('export', '')

    logs = AuditLog.objects.select_related('pengguna').all()

    if query:
        logs = logs.filter(
            Q(aksi__icontains=query) | 
            Q(keterangan__icontains=query)
        )
    if pengguna_id:
        logs = logs.filter(pengguna_id=pengguna_id)

    if export == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="audit_aktivitas.csv"'
        writer = csv.writer(response)
        writer.writerow(['Waktu', 'Pengguna', 'Aksi', 'Keterangan'])
        for log in logs:
            pengguna_nama = log.pengguna.username if log.pengguna else 'Sistem'
            waktu_str = log.waktu.strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([waktu_str, pengguna_nama, log.aksi, log.keterangan])
        return response

    paginator = Paginator(logs, 25) # 25 per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    users = User.objects.filter(is_active=True).order_by('username')

    return render(request, 'POSsystem/audit.html', {
        'page_obj': page_obj,
        'query': query,
        'pengguna_id': pengguna_id,
        'users': users
    })


@login_required
@group_required('Owner', 'Supervisor')
def halaman_riwayat_transaksi(request):
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    if start_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    else:
        start_date = timezone.localdate() - timedelta(days=6)

    if end_date_str:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    else:
        end_date = timezone.localdate()

    transaksi_qs = Transaksi.objects.filter(
        tanggal__date__gte=start_date,
        tanggal__date__lte=end_date
    ).prefetch_related('items__produk').order_by('-tanggal')

    grouped_data = {}
    for trx in transaksi_qs:
        date_str = trx.tanggal.date().strftime('%Y-%m-%d')
        if date_str not in grouped_data:
            grouped_data[date_str] = {
                'tanggal': trx.tanggal.date(),
                'pendapatan': 0,
                'keuntungan': 0,
                'jumlah_transaksi': 0
            }
        
        grouped_data[date_str]['pendapatan'] += trx.total_harga
        
        trx_keuntungan = sum(
            item.subtotal - (item.harga_modal * item.jumlah) 
            for item in trx.items.all()
        )
        grouped_data[date_str]['keuntungan'] += trx_keuntungan
        grouped_data[date_str]['jumlah_transaksi'] += 1

    # Convert to list and sort descending by date (already sorted mostly because of order_by but let's be sure)
    riwayat_harian = sorted(grouped_data.values(), key=lambda x: x['tanggal'], reverse=True)

    # For charts (we can just show them for the same filtered query or globally, let's keep it global excluding today for backwards compatibility, or maybe just exclude nothing since the user might want charts for all time)
    # The previous code excluded today. We'll leave it as is to not break the chart if they liked it.
    transaksi_sebelumnya = Transaksi.objects.exclude(tanggal__date=timezone.localdate()).order_by('-tanggal')

    def ringkas_periode(truncate):
        hasil = transaksi_sebelumnya.annotate(periode=truncate('tanggal')).values('periode').annotate(
            total=Sum('total_harga'), jumlah=Count('id')
        ).order_by('periode')
        return [
            {
                'label': row['periode'].strftime('%d %b %Y'),
                'total': float(row['total'] or 0),
                'jumlah': row['jumlah'],
            }
            for row in hasil
        ]

    laporan_bulanan = ringkas_periode(TruncMonth)
    laporan_mingguan = ringkas_periode(TruncWeek)
    laporan_tahunan = ringkas_periode(TruncYear)
    
    return render(request, 'POSsystem/riwayat_transaksi.html', {
        'riwayat_harian': riwayat_harian,
        'start_date': start_date.strftime('%Y-%m-%d'),
        'end_date': end_date.strftime('%Y-%m-%d'),
        'laporan_bulanan': laporan_bulanan,
        'laporan_mingguan': laporan_mingguan,
        'laporan_tahunan': laporan_tahunan,
        'laporan_bulanan_json': json.dumps(laporan_bulanan),
        'laporan_mingguan_json': json.dumps(laporan_mingguan),
        'laporan_tahunan_json': json.dumps(laporan_tahunan),
    })

@login_required
@group_required('Owner', 'Supervisor')
def halaman_riwayat_harian(request, date_str):
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Format tanggal tidak valid.')
        return redirect('halaman_riwayat_transaksi')
    
    transaksi_list = Transaksi.objects.filter(
        tanggal__date=target_date
    ).prefetch_related('items__produk').order_by('-tanggal')

    return render(request, 'POSsystem/riwayat_harian.html', {
        'target_date': target_date,
        'transaksi_list': transaksi_list,
    })

@login_required
@group_required('Owner', 'Supervisor')
def halaman_mutasi_stok(request):
    tanggal_str = request.GET.get('tanggal')
    if tanggal_str:
        try:
            tanggal = datetime.strptime(tanggal_str, '%Y-%m-%d').date()
        except ValueError:
            tanggal = timezone.localdate()
    else:
        tanggal = timezone.localdate()

    mutasi = []
    
    # 1. Penjualan (DetailTransaksi)
    penjualan = DetailTransaksi.objects.filter(
        transaksi__tanggal__date=tanggal,
        transaksi__status='SELESAI'
    ).select_related('produk', 'transaksi')
    for p in penjualan:
        mutasi.append({
            'waktu': p.transaksi.tanggal,
            'produk': p.produk,
            'aktivitas': f'Penjualan (Kasir) - Trx #{p.transaksi.kode_transaksi}',
            'tipe': 'OUT',
            'jumlah': p.jumlah
        })

    # 2. Pembelian
    pembelian = Pembelian.objects.filter(tanggal__date=tanggal).select_related('produk')
    for p in pembelian:
        mutasi.append({
            'waktu': p.tanggal,
            'produk': p.produk,
            'aktivitas': 'Pembelian (Supplier)',
            'tipe': 'IN',
            'jumlah': p.jumlah
        })

    # 3. Retur Transaksi
    retur = ReturTransaksi.objects.filter(tanggal__date=tanggal).select_related('detail__produk')
    for r in retur:
        mutasi.append({
            'waktu': r.tanggal,
            'produk': r.detail.produk,
            'aktivitas': 'Retur Penjualan',
            'tipe': 'IN',
            'jumlah': r.jumlah
        })

    # 4. Stok Opname
    opname = StokOpname.objects.filter(tanggal__date=tanggal).select_related('produk')
    for o in opname:
        if o.selisih != 0:
            mutasi.append({
                'waktu': o.tanggal,
                'produk': o.produk,
                'aktivitas': f'Stok Opname ({o.keterangan})',
                'tipe': 'IN' if o.selisih > 0 else 'OUT',
                'jumlah': abs(o.selisih)
            })

    # 5. Riwayat Stok (Penyesuaian manual)
    riwayat = RiwayatStok.objects.filter(tanggal__date=tanggal).select_related('produk')
    for r in riwayat:
        mutasi.append({
            'waktu': r.tanggal,
            'produk': r.produk,
            'aktivitas': f'Penyesuaian Manual ({r.keterangan})',
            'tipe': 'IN' if r.tipe == 'MASUK' else 'OUT',
            'jumlah': r.jumlah
        })

    # Sort mutasi by waktu descending
    mutasi.sort(key=lambda x: x['waktu'], reverse=True)

    return render(request, 'POSsystem/mutasi_stok.html', {
        'mutasi': mutasi,
        'tanggal_formatted': tanggal.strftime('%Y-%m-%d'),
    })
