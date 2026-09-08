import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import DetailTransaksi, Kategori, Produk, Transaksi


class PosFlowTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='kasir', password='rahasia')
		self.client.login(username='kasir', password='rahasia')
		self.produk = Produk.objects.create(
			kategori=Kategori.objects.create(nama='Minuman'),
			kode_barcode='8990001',
			nama_produk='Teh Botol',
			harga_beli=Decimal('3000'),
			harga_jual=Decimal('5000'),
			stok_gudang=10,
		)

	def add_cart(self, jumlah=1):
		session = self.client.session
		session['keranjang'] = {str(self.produk.id): {
			'id': self.produk.id, 'nama': self.produk.nama_produk,
			'harga': 5000.0, 'jumlah': jumlah, 'subtotal': 5000.0 * jumlah,
		}}
		session.save()

	def test_checkout_stores_payment_and_reduces_stock(self):
		self.add_cart(2)
		response = self.client.post(reverse('checkout'), {'uang_dibayar': '15000'})
		self.assertEqual(response.status_code, 302)
		self.produk.refresh_from_db()
		transaksi = Transaksi.objects.get()
		self.assertEqual(transaksi.uang_dibayar, Decimal('15000'))
		self.assertEqual(transaksi.kembalian, Decimal('5000'))
		self.assertEqual(self.produk.stok_gudang, 8)
		self.assertEqual(DetailTransaksi.objects.count(), 1)

	def test_underpayment_does_not_create_transaction(self):
		self.add_cart(2)
		self.client.post(reverse('checkout'), {'uang_dibayar': '5000'})
		self.assertEqual(Transaksi.objects.count(), 0)
		self.produk.refresh_from_db()
		self.assertEqual(self.produk.stok_gudang, 10)

	def test_login_required_for_cashier(self):
		self.client.logout()
		response = self.client.get(reverse('halaman_kasir'))
		self.assertRedirects(response, '/login/?next=/')


class PaymentGatewayTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='kasir', password='rahasia')
		self.client.login(username='kasir', password='rahasia')
		self.produk = Produk.objects.create(
			kategori=Kategori.objects.create(nama='Minuman'),
			kode_barcode='8990001',
			nama_produk='Teh Botol',
			harga_beli=Decimal('3000'),
			harga_jual=Decimal('5000'),
			stok_gudang=10,
		)

	def buat_transaksi_pending(self):
		return Transaksi.objects.create(
			kode_transaksi='TRX-TEST0001',
			total_harga=Decimal('10000'),
			metode_pembayaran='QRIS',
			status='PENDING',
		)

	def payload_notifikasi(self, transaksi, transaction_status, **extra):
		gross_amount = f'{transaksi.total_harga:.2f}'
		payload = {
			'order_id': transaksi.kode_transaksi,
			'status_code': '200',
			'gross_amount': gross_amount,
			'transaction_status': transaction_status,
			'payment_type': 'qris',
			**extra,
		}
		payload['signature_key'] = hashlib.sha512(
			f"{payload['order_id']}{payload['status_code']}{gross_amount}{settings.MIDTRANS_SERVER_KEY}".encode()
		).hexdigest()
		return payload

	def kirim_notifikasi(self, payload):
		return self.client.post(
			reverse('midtrans_notification'),
			data=json.dumps(payload),
			content_type='application/json',
		)

	def test_webhook_menolak_signature_tidak_valid(self):
		transaksi = self.buat_transaksi_pending()
		payload = self.payload_notifikasi(transaksi, 'settlement')
		payload['signature_key'] = 'signature-palsu'
		response = self.kirim_notifikasi(payload)
		self.assertEqual(response.status_code, 403)
		transaksi.refresh_from_db()
		self.assertEqual(transaksi.status, 'PENDING')

	def test_webhook_settlement_menyelesaikan_transaksi(self):
		transaksi = self.buat_transaksi_pending()
		response = self.kirim_notifikasi(self.payload_notifikasi(transaksi, 'settlement'))
		self.assertEqual(response.status_code, 200)
		transaksi.refresh_from_db()
		self.assertEqual(transaksi.status, 'SELESAI')
		self.assertEqual(transaksi.uang_dibayar, Decimal('10000'))
		self.assertEqual(transaksi.tipe_pembayaran_gateway, 'qris')
		self.assertIsNotNone(transaksi.waktu_bayar)

	def test_webhook_capture_dengan_fraud_challenge_tetap_pending(self):
		transaksi = self.buat_transaksi_pending()
		response = self.kirim_notifikasi(
			self.payload_notifikasi(transaksi, 'capture', fraud_status='challenge')
		)
		self.assertEqual(response.status_code, 200)
		transaksi.refresh_from_db()
		self.assertEqual(transaksi.status, 'PENDING')

	def test_webhook_expire_mengembalikan_stok(self):
		transaksi = self.buat_transaksi_pending()
		DetailTransaksi.objects.create(
			transaksi=transaksi, produk=self.produk, jumlah=2,
			subtotal=Decimal('10000'), harga_modal=Decimal('3000'),
		)
		self.produk.stok_gudang -= 2
		self.produk.save(update_fields=['stok_gudang'])
		response = self.kirim_notifikasi(self.payload_notifikasi(transaksi, 'expire'))
		self.assertEqual(response.status_code, 200)
		transaksi.refresh_from_db()
		self.produk.refresh_from_db()
		self.assertEqual(transaksi.status, 'VOID')
		self.assertEqual(self.produk.stok_gudang, 10)

	def test_checkout_online_membuat_transaksi_pending(self):
		session = self.client.session
		session['keranjang'] = {str(self.produk.id): {
			'id': self.produk.id, 'nama': self.produk.nama_produk,
			'harga': 5000.0, 'jumlah': 2, 'subtotal': 10000.0,
		}}
		session.save()
		with patch('POSsystem.views.buat_snap_token', return_value='token-uji'):
			response = self.client.post(reverse('checkout'), {'metode_pembayaran': 'QRIS'})
		transaksi = Transaksi.objects.get()
		self.assertRedirects(response, reverse('halaman_pembayaran', args=[transaksi.id]))
		self.assertEqual(transaksi.status, 'PENDING')
		self.assertEqual(transaksi.snap_token, 'token-uji')
		self.produk.refresh_from_db()
		self.assertEqual(self.produk.stok_gudang, 8)

	def test_checkout_online_gagal_token_membatalkan_dan_kembalikan_stok(self):
		session = self.client.session
		session['keranjang'] = {str(self.produk.id): {
			'id': self.produk.id, 'nama': self.produk.nama_produk,
			'harga': 5000.0, 'jumlah': 1, 'subtotal': 5000.0,
		}}
		session.save()
		with patch('POSsystem.views.buat_snap_token', side_effect=Exception('Midtrans down')):
			response = self.client.post(reverse('checkout'), {'metode_pembayaran': 'TRANSFER'})
		self.assertRedirects(response, reverse('halaman_kasir'))
		transaksi = Transaksi.objects.get()
		self.assertEqual(transaksi.status, 'VOID')
		self.produk.refresh_from_db()
		self.assertEqual(self.produk.stok_gudang, 10)
