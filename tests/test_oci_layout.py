"""Tests for OCI Image Layout structure validation."""
import json, tempfile, shutil
from pathlib import Path
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

def test_oci_layout_structure():
    """Verify complete OCI layout structure is created."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output=Path(tmpdir)/"test-image"
        cfg=BuildConfig(tag="test:v1",output_dir=str(output),context_dir=".")
        builder=ImageBuilder(cfg)
        builder.build()
        
        assert (output/"oci-layout").exists(),"oci-layout file missing"
        assert (output/"index.json").exists(),"index.json missing"
        assert (output/"blobs"/"sha256").exists(),"blobs/sha256/ directory missing"
        assert (output/"refs"/"tags").exists(),"refs/tags/ directory missing"
        
        layout=json.loads((output/"oci-layout").read_text())
        assert layout["imageLayoutVersion"]=="1.0.0","Invalid oci-layout version"
        
        index=json.loads((output/"index.json").read_text())
        assert index["schemaVersion"]==2,"Invalid index schema version"
        assert index["mediaType"]=="application/vnd.oci.image.index.v1+json"
        assert len(index["manifests"])==1,"Expected 1 manifest"
        assert index["annotations"]["org.opencontainers.image.ref.name"]=="test:v1"
        
        manifest_desc=index["manifests"][0]
        assert manifest_desc["platform"]["architecture"]=="amd64"
        assert manifest_desc["platform"]["os"]=="linux"
        
        tag_ref=(output/"refs"/"tags"/"v1").read_text().strip()
        assert tag_ref==manifest_desc["digest"],"Tag reference doesn't match manifest digest"
        
        manifest_blob=output/"blobs"/"sha256"/manifest_desc["digest"].split(":",1)[1]
        assert manifest_blob.exists(),"Manifest blob not found"
        
        manifest=json.loads(manifest_blob.read_text())
        assert manifest["mediaType"]=="application/vnd.oci.image.manifest.v1+json"
        assert len(manifest["layers"])>=1,"Expected at least 1 layer"

def test_tag_extraction():
    """Verify tag name is correctly extracted for refs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output=Path(tmpdir)/"test-image"
        cfg=BuildConfig(tag="myapp:v2.1.0",output_dir=str(output),context_dir=".")
        builder=ImageBuilder(cfg)
        builder.build()
        
        assert (output/"refs"/"tags"/"v2.1.0").exists(),"Tag file not created with correct name"
        
        cfg2=BuildConfig(tag="latest",output_dir=str(output)+"2",context_dir=".")
        builder2=ImageBuilder(cfg2)
        builder2.build()
        assert (Path(output).parent/"test-image2"/"refs"/"tags"/"latest").exists()

if __name__=="__main__":
    test_oci_layout_structure()
    test_tag_extraction()
    print("✅ All OCI layout tests passed")
