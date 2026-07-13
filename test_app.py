import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto_utils import encrypt, decrypt, is_encrypted


class TestCryptoUtils(unittest.TestCase):
    
    def setUp(self):
        os.environ['FLASK_SECRET_KEY'] = 'test_secret_key_that_is_long_enough_for_aes_256'
    
    def test_encrypt_empty_string(self):
        result = encrypt('')
        self.assertEqual(result, '')
    
    def test_decrypt_empty_string(self):
        result = decrypt('')
        self.assertEqual(result, '')
    
    def test_encrypt_decrypt_roundtrip(self):
        plaintext = 'test_password_123'
        encrypted = encrypt(plaintext)
        self.assertTrue(is_encrypted(encrypted))
        decrypted = decrypt(encrypted)
        self.assertEqual(decrypted, plaintext)
    
    def test_encrypt_decrypt_special_chars(self):
        plaintext = 'test@#$%^&*()_+-=[]{}|;:,.<>?'
        encrypted = encrypt(plaintext)
        decrypted = decrypt(encrypted)
        self.assertEqual(decrypted, plaintext)
    
    def test_decrypt_non_encrypted_string(self):
        plaintext = 'not_encrypted_value'
        result = decrypt(plaintext)
        self.assertEqual(result, plaintext)
    
    def test_is_encrypted(self):
        self.assertTrue(is_encrypted('ENC[some_encoded_data]'))
        self.assertFalse(is_encrypted('plain_text'))
        self.assertFalse(is_encrypted('ENC[incomplete'))


class TestFlaskApp(unittest.TestCase):
    
    def setUp(self):
        os.environ['FLASK_SECRET_KEY'] = 'test_secret_key_for_testing'
        os.environ['APP_PASSWORD'] = 'test_password'
        from app import app
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        self.app = app.test_client()
    
    def test_health_endpoint(self):
        response = self.app.get('/health')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn('status', data)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('version', data)
    
    def test_login_success(self):
        response = self.app.post('/login', data={'password': 'test_password'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/'))
    
    def test_login_failure(self):
        response = self.app.post('/login', data={'password': 'wrong_password'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('密码错误'.encode('utf-8'), response.data)
    
    def test_logout(self):
        self.app.post('/login', data={'password': 'test_password'})
        response = self.app.get('/logout')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/login'))
    
    def test_protected_route_requires_login(self):
        response = self.app.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)


if __name__ == '__main__':
    unittest.main()
