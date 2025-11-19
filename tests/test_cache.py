"""Tests for layer caching system."""
import tempfile; import shutil; import time
from pathlib import Path
from pycontainer.cache import LayerCache

def test_cache_miss_then_hit():
    """Test cache miss followed by cache hit."""
    with tempfile.TemporaryDirectory() as td:
        cache_dir=Path(td)/"cache"; cache=LayerCache(cache_dir)
        
        # Create test files
        src=Path(td)/"src"; src.mkdir()
        (src/"a.py").write_text("print('a')")
        (src/"b.py").write_text("print('b')")
        files=[(src/"a.py",Path("a.py")),(src/"b.py",Path("b.py"))]
        
        # Cache miss
        result=cache.get_layer(files)
        assert result is None,"Expected cache miss on first lookup"
        
        # Store layer
        layer_tar=Path(td)/"layer.tar"
        layer_tar.write_bytes(b"fake tar content")
        cache.store_layer(files, "sha256:abc123", layer_tar)
        
        # Cache hit
        result=cache.get_layer(files)
        assert result is not None,"Expected cache hit after store"
        digest, cache_path=result
        assert digest=="sha256:abc123"
        assert cache_path.read_bytes()==b"fake tar content"

def test_cache_invalidation_on_content_change():
    """Test cache invalidation when file content changes."""
    with tempfile.TemporaryDirectory() as td:
        cache_dir=Path(td)/"cache"; cache=LayerCache(cache_dir)
        
        # Create test file
        src=Path(td)/"src"; src.mkdir()
        test_file=src/"test.py"; test_file.write_text("v1")
        files=[(test_file, Path("test.py"))]
        
        # Store layer v1
        layer_tar=Path(td)/"layer.tar"
        layer_tar.write_bytes(b"layer v1")
        cache.store_layer(files, "sha256:v1", layer_tar)
        
        # Cache hit
        result=cache.get_layer(files)
        assert result is not None
        assert result[0]=="sha256:v1"
        
        # Modify file (change mtime and size)
        time.sleep(0.01)  # Ensure mtime differs
        test_file.write_text("v2_longer")
        
        # Cache miss (file changed)
        result=cache.get_layer(files)
        assert result is None,"Expected cache miss after file modification"

def test_lru_eviction():
    """Test LRU eviction when cache exceeds max size."""
    with tempfile.TemporaryDirectory() as td:
        cache_dir=Path(td)/"cache"
        cache=LayerCache(cache_dir, max_size_mb=0.001)  # 1KB limit
        
        # Create test files
        src=Path(td)/"src"; src.mkdir()
        (src/"a.py").write_text("a")
        (src/"b.py").write_text("b")
        (src/"c.py").write_text("c")
        
        files_a=[(src/"a.py",Path("a.py"))]
        files_b=[(src/"b.py",Path("b.py"))]
        files_c=[(src/"c.py",Path("c.py"))]
        
        # Store 3 layers (600 bytes each)
        for i, (files, digest) in enumerate([
            (files_a, "sha256:aaa"),
            (files_b, "sha256:bbb"),
            (files_c, "sha256:ccc")
        ]):
            layer_tar=Path(td)/f"layer{i}.tar"
            layer_tar.write_bytes(b"x"*600)  # 600 bytes
            cache.store_layer(files, digest, layer_tar)
            time.sleep(0.01)  # Ensure different access times
        
        # Cache should have evicted oldest layers (exceeds 1KB limit)
        blobs_dir=cache_dir/"blobs"/"sha256"
        remaining_blobs=list(blobs_dir.glob("*")) if blobs_dir.exists() else []
        
        # Should have evicted some layers to stay under limit
        total_size=sum(b.stat().st_size for b in remaining_blobs)
        assert total_size<=1024,"Cache should evict to stay under limit"

def test_cache_with_different_file_lists():
    """Test that different file lists get different cache entries."""
    with tempfile.TemporaryDirectory() as td:
        cache_dir=Path(td)/"cache"; cache=LayerCache(cache_dir)
        
        # Create test files
        src=Path(td)/"src"; src.mkdir()
        (src/"a.py").write_text("a")
        (src/"b.py").write_text("b")
        
        files_a=[(src/"a.py",Path("a.py"))]
        files_ab=[(src/"a.py",Path("a.py")),(src/"b.py",Path("b.py"))]
        
        # Store layer for files_a
        layer_tar=Path(td)/"layer_a.tar"
        layer_tar.write_bytes(b"layer a")
        cache.store_layer(files_a, "sha256:aaa", layer_tar)
        
        # Different file list should miss cache
        result=cache.get_layer(files_ab)
        assert result is None,"Different file list should miss cache"
        
        # Same file list should hit cache
        result=cache.get_layer(files_a)
        assert result is not None,"Same file list should hit cache"

def test_cache_disabled():
    """Test that None cache_dir disables caching."""
    cache=LayerCache(None)
    
    # All operations should be no-ops
    files=[(Path("test.py"), Path("test.py"))]
    
    result=cache.get_layer(files)
    assert result is None,"Disabled cache should always return None"
    
    # store_layer should not raise
    cache.store_layer(files, "sha256:test", Path("fake.tar"))
