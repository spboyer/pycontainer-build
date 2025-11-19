"""Layer caching and content-addressable storage."""
import hashlib, json, shutil, time
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict

@dataclass
class CacheEntry:
    digest: str
    size: int
    created: float
    last_used: float
    source_files: List[str]
    
    def touch(self):
        """Update last used timestamp."""
        self.last_used=time.time()

class LayerCache:
    def __init__(self, cache_dir: Optional[Path]=None, max_size_mb: int=5000):
        if cache_dir is None:
            self.enabled=False
            self.cache_dir=None
            self.blobs_dir=None
            self.index_file=None
            self.max_size_bytes=0
            self.index={}
            return
        
        self.enabled=True
        self.cache_dir=cache_dir if isinstance(cache_dir, Path) else Path(cache_dir)
        self.blobs_dir=self.cache_dir/'blobs'/'sha256'
        self.index_file=self.cache_dir/'index.json'
        self.max_size_bytes=max_size_mb * 1024 * 1024
        self._ensure_structure()
        self.index=self._load_index()
    
    def _ensure_structure(self):
        """Create cache directory structure."""
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_index(self) -> Dict[str, CacheEntry]:
        """Load cache index from disk."""
        if not self.index_file.exists():
            return {}
        try:
            data=json.loads(self.index_file.read_text())
            return {k: CacheEntry(**v) for k, v in data.items()}
        except:
            return {}
    
    def _save_index(self):
        """Save cache index to disk."""
        data={k: asdict(v) for k, v in self.index.items()}
        self.index_file.write_text(json.dumps(data, indent=2))
    
    def _compute_files_digest(self, files: List[Tuple[Path, Path]]) -> str:
        """Compute digest of file list (paths + sizes + mtimes)."""
        h=hashlib.sha256()
        for abs_path, rel_path in sorted(files, key=lambda x: x[1].as_posix()):
            h.update(rel_path.as_posix().encode())
            if abs_path.exists():
                stat=abs_path.stat()
                h.update(str(stat.st_size).encode())
                h.update(str(int(stat.st_mtime)).encode())
        return f"sha256:{h.hexdigest()}"
    
    def get_layer(self, files: List[Tuple[Path, Path]]) -> Optional[Tuple[str, Path]]:
        """Get cached layer for file list, returns (digest, path) or None."""
        if not self.enabled:
            return None
        
        files_digest=self._compute_files_digest(files)
        
        if files_digest in self.index:
            entry=self.index[files_digest]
            blob_path=self.blobs_dir/entry.digest.split(':',1)[1]
            
            if blob_path.exists():
                entry.touch()
                self._save_index()
                return (entry.digest, blob_path)
            else:
                del self.index[files_digest]
                self._save_index()
        
        return None
    
    def store_layer(self, files: List[Tuple[Path, Path]], digest: str, layer_path: Path) -> Path:
        """Store layer in cache, returns cache path."""
        if not self.enabled:
            return layer_path
        
        files_digest=self._compute_files_digest(files)
        blob_path=self.blobs_dir/digest.split(':',1)[1]
        
        if not blob_path.exists():
            shutil.copy2(layer_path, blob_path)
        
        source_files=[str(rel) for _, rel in files]
        entry=CacheEntry(
            digest=digest,
            size=blob_path.stat().st_size,
            created=time.time(),
            last_used=time.time(),
            source_files=source_files
        )
        
        self.index[files_digest]=entry
        self._save_index()
        self._evict_if_needed()
        
        return blob_path
    
    def _evict_if_needed(self):
        """Evict old entries if cache exceeds max size (LRU)."""
        total_size=sum(e.size for e in self.index.values())
        
        if total_size <= self.max_size_bytes:
            return
        
        entries=sorted(self.index.items(), key=lambda x: x[1].last_used)
        
        for files_digest, entry in entries:
            if total_size <= self.max_size_bytes * 0.8:
                break
            
            blob_path=self.blobs_dir/entry.digest.split(':',1)[1]
            if blob_path.exists():
                blob_path.unlink()
            
            total_size -= entry.size
            del self.index[files_digest]
        
        self._save_index()
    
    def clear(self):
        """Clear entire cache."""
        if self.blobs_dir.exists():
            shutil.rmtree(self.blobs_dir)
        if self.index_file.exists():
            self.index_file.unlink()
        self._ensure_structure()
        self.index={}
    
    def get_stats(self) -> Dict:
        """Get cache statistics."""
        total_size=sum(e.size for e in self.index.values())
        return {
            'entries': len(self.index),
            'total_size_mb': total_size / (1024 * 1024),
            'max_size_mb': self.max_size_bytes / (1024 * 1024),
            'usage_percent': (total_size / self.max_size_bytes * 100) if self.max_size_bytes > 0 else 0
        }
