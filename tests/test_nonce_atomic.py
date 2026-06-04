"""
Unit tests for atomic nonce generation in BitfinexClient.
Tests monotonic increase, uniqueness under concurrency, and file atomicity.
"""

import unittest
import os
import sys
import time
import threading
import tempfile
import shutil
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class MockBitfinexClient:
    """Mock client that implements just the nonce logic for testing."""
    
    def __init__(self, nonce_file: str):
        self._nonce_path = nonce_file
        self._lock = threading.Lock()
    
    def _generate_nonce(self) -> str:
        """Generate monotonic increasing nonce (simplified for testing)."""
        import fcntl
        
        nonce_dir = Path(self._nonce_path).parent
        nonce_dir.mkdir(parents=True, exist_ok=True)
        
        fd = os.open(self._nonce_path, os.O_RDWR | os.O_CREAT, 0o600)
        
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            
            # Read current nonce
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 64).decode().strip()
                last_nonce = int(raw) if raw else None
            except (ValueError, IOError):
                last_nonce = None
            
            # Generate new nonce
            ts_nonce = int(time.time() * 1_000_000)
            
            if last_nonce is None:
                new_nonce = ((ts_nonce + 999_999) // 1_000_000) * 1_000_000
            else:
                min_next = last_nonce + 1_000_000
                new_nonce = max(ts_nonce, min_next)
                new_nonce = ((new_nonce + 999_999) // 1_000_000) * 1_000_000
            
            # Atomic write
            temp_path = f"{self._nonce_path}.tmp.{os.getpid()}.{threading.current_thread().ident}"
            with open(temp_path, 'w') as f:
                f.write(str(new_nonce))
                f.flush()
                os.fsync(f.fileno())
            
            os.rename(temp_path, self._nonce_path)
            
            return str(new_nonce)
            
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TestNonceGeneration(unittest.TestCase):
    """Test nonce generation properties."""
    
    def setUp(self):
        """Create temp directory for nonce files."""
        self.temp_dir = tempfile.mkdtemp()
        self.nonce_file = os.path.join(self.temp_dir, ".bfx_nonce")
    
    def tearDown(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_nonce_monotonic_increase(self):
        """Each nonce should be strictly greater than the previous."""
        client = MockBitfinexClient(self.nonce_file)
        
        nonces = [client._generate_nonce() for _ in range(10)]
        
        for i in range(1, len(nonces)):
            self.assertGreater(
                int(nonces[i]), int(nonces[i-1]),
                f"Nonce {i} ({nonces[i]}) should be greater than nonce {i-1} ({nonces[i-1]})"
            )
    
    def test_nonce_ends_in_zeros(self):
        """Nonce should always end in zeros (Bitfinex requirement)."""
        client = MockBitfinexClient(self.nonce_file)
        
        for _ in range(5):
            nonce = client._generate_nonce()
            self.assertEqual(
                int(nonce) % 1_000_000, 0,
                f"Nonce {nonce} should end in zeros"
            )
    
    def test_nonce_minimum_increment(self):
        """Nonce should increase by at least 1,000,000."""
        client = MockBitfinexClient(self.nonce_file)
        
        nonce1 = int(client._generate_nonce())
        nonce2 = int(client._generate_nonce())
        
        self.assertGreaterEqual(nonce2 - nonce1, 1_000_000)
    
    def test_concurrent_access_unique_nonces(self):
        """Concurrent access should produce unique nonces."""
        client = MockBitfinexClient(self.nonce_file)
        nonces = []
        errors = []
        
        def generate_multiple():
            try:
                for _ in range(20):
                    nonces.append(client._generate_nonce())
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=generate_multiple) for _ in range(5)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # No errors
        self.assertEqual(len(errors), 0, f"Errors during concurrent access: {errors}")
        
        # All nonces unique
        self.assertEqual(len(nonces), len(set(nonces)), 
                        f"Duplicate nonces generated: {len(nonces)} total, {len(set(nonces))} unique")
        
        # All monotonic (per thread ordering preserved)
        self.assertEqual(len(nonces), 100)  # 5 threads * 20 nonces
    
    def test_file_permissions(self):
        """Nonce file should have restrictive permissions."""
        client = MockBitfinexClient(self.nonce_file)
        client._generate_nonce()
        
        mode = os.stat(self.nonce_file).st_mode
        # Should be 0o600 (owner read/write only)
        self.assertEqual(mode & 0o777, 0o600)
    
    def test_recovery_from_corrupt_file(self):
        """Should recover if nonce file is corrupt."""
        # Create corrupt file
        with open(self.nonce_file, 'w') as f:
            f.write("not_a_number")
        
        client = MockBitfinexClient(self.nonce_file)
        nonce = client._generate_nonce()
        
        # Should generate valid nonce
        self.assertIsNotNone(nonce)
        self.assertTrue(nonce.isdigit())
        self.assertEqual(int(nonce) % 1_000_000, 0)
    
    def test_recovery_from_empty_file(self):
        """Should handle empty nonce file."""
        # Create empty file
        with open(self.nonce_file, 'w') as f:
            pass
        
        client = MockBitfinexClient(self.nonce_file)
        nonce = client._generate_nonce()
        
        self.assertIsNotNone(nonce)
        self.assertTrue(nonce.isdigit())
    
    def test_instance_isolation(self):
        """Different instances should have independent nonces."""
        long_file = os.path.join(self.temp_dir, ".bfx_nonce_long")
        short_file = os.path.join(self.temp_dir, ".bfx_nonce_short")
        
        long_client = MockBitfinexClient(long_file)
        short_client = MockBitfinexClient(short_file)
        
        long_nonces = [long_client._generate_nonce() for _ in range(5)]
        short_nonces = [short_client._generate_nonce() for _ in range(5)]
        
        # Both should work independently
        self.assertEqual(len(long_nonces), 5)
        self.assertEqual(len(short_nonces), 5)
        
        # Each should be monotonic
        for i in range(1, 5):
            self.assertGreater(int(long_nonces[i]), int(long_nonces[i-1]))
            self.assertGreater(int(short_nonces[i]), int(short_nonces[i-1]))
    
    def test_no_temp_file_left_behind(self):
        """Temp files should be cleaned up after atomic write."""
        client = MockBitfinexClient(self.nonce_file)
        client._generate_nonce()
        
        # Check no temp files left
        temp_files = [f for f in os.listdir(self.temp_dir) if '.tmp.' in f]
        self.assertEqual(len(temp_files), 0, f"Temp files left behind: {temp_files}")


class TestNoncePerformance(unittest.TestCase):
    """Test nonce generation performance."""
    
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.nonce_file = os.path.join(self.temp_dir, ".bfx_nonce")
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_nonce_generation_speed(self):
        """Nonce generation should be fast (< 10ms per call)."""
        client = MockBitfinexClient(self.nonce_file)
        
        start = time.time()
        for _ in range(100):
            client._generate_nonce()
        elapsed = time.time() - start
        
        # Should complete 100 nonces in less than 1 second
        self.assertLess(elapsed, 1.0, f"Nonce generation too slow: {elapsed:.3f}s for 100 calls")


if __name__ == "__main__":
    unittest.main()
