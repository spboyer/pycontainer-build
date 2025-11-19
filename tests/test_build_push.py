"""Integration test demonstrating build and push workflow."""
import tempfile, json
from pathlib import Path
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

def test_build_and_push_workflow():
    """Test complete build workflow preparing for push."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output=Path(tmpdir)/"test-image"
        
        cfg=BuildConfig(
            tag="localhost:5000/testapp:v1",
            output_dir=str(output),
            context_dir=".",
            env={"APP_ENV":"production"}
        )
        
        builder=ImageBuilder(cfg)
        result=builder.build()
        
        assert result=="localhost:5000/testapp:v1"
        assert hasattr(builder,'manifest_digest')
        assert hasattr(builder,'config_digest')
        assert hasattr(builder,'layers')
        assert len(builder.layers)>=1
        
        layers_dir=output/'blobs'/'sha256'
        
        manifest_blob=layers_dir/builder.manifest_digest.split(":",1)[1]
        assert manifest_blob.exists(),"Manifest blob not found"
        
        config_blob=layers_dir/builder.config_digest.split(":",1)[1]
        assert config_blob.exists(),"Config blob not found"
        
        for layer in builder.layers:
            layer_blob=layers_dir/layer.digest.split(":",1)[1]
            assert layer_blob.exists(),f"Layer blob {layer.digest} not found"
        
        print(f"✓ Built image ready to push")
        print(f"  Manifest: {builder.manifest_digest[:19]}...")
        print(f"  Config: {builder.config_digest[:19]}...")
        print(f"  Layers: {len(builder.layers)}")

def test_push_method_exists():
    """Verify push method is available on ImageBuilder."""
    cfg=BuildConfig(tag="test:v1",context_dir=".")
    builder=ImageBuilder(cfg)
    
    assert hasattr(builder,'push'),"push() method not found"
    assert callable(builder.push),"push is not callable"

if __name__=="__main__":
    test_build_and_push_workflow()
    test_push_method_exists()
    print("✅ All build/push workflow tests passed")
