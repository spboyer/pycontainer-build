"""Software Bill of Materials (SBOM) generation."""
import json, hashlib, subprocess
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone

def generate_sbom(context_dir: Path, output_path: Path, format: str="spdx") -> Dict:
    """Generate SBOM in SPDX or CycloneDX format.
    
    Args:
        context_dir: Project context directory
        output_path: Where to save SBOM JSON
        format: 'spdx' or 'cyclonedx'
    
    Returns:
        SBOM dictionary
    """
    if format=="spdx":
        sbom=_generate_spdx(context_dir)
    elif format=="cyclonedx":
        sbom=_generate_cyclonedx(context_dir)
    else:
        raise ValueError(f"Unsupported SBOM format: {format}")
    
    output_path.write_text(json.dumps(sbom, indent=2))
    return sbom

def _generate_spdx(context_dir: Path) -> Dict:
    """Generate SPDX 2.3 format SBOM."""
    packages=_get_python_packages(context_dir)
    
    sbom={
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"pycontainer-{context_dir.name}",
        "documentNamespace": f"https://sbom.pycontainer/{context_dir.name}/{_generate_doc_id()}",
        "creationInfo": {
            "created": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "creators": ["Tool: pycontainer-build"],
            "licenseListVersion": "3.21"
        },
        "packages": []
    }
    
    for pkg_name, version in packages:
        sbom["packages"].append({
            "SPDXID": f"SPDXRef-Package-{pkg_name}",
            "name": pkg_name,
            "versionInfo": version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION"
        })
    
    return sbom

def _generate_cyclonedx(context_dir: Path) -> Dict:
    """Generate CycloneDX 1.4 format SBOM."""
    packages=_get_python_packages(context_dir)
    
    sbom={
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": f"urn:uuid:{_generate_doc_id()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "tools": [{"name": "pycontainer-build", "version": "0.1.0"}]
        },
        "components": []
    }
    
    for pkg_name, version in packages:
        sbom["components"].append({
            "type": "library",
            "name": pkg_name,
            "version": version,
            "purl": f"pkg:pypi/{pkg_name}@{version}"
        })
    
    return sbom

def _get_python_packages(context_dir: Path) -> List[tuple]:
    """Get list of installed Python packages."""
    packages=[]
    
    requirements=context_dir/"requirements.txt"
    if requirements.exists():
        for line in requirements.read_text().splitlines():
            line=line.strip()
            if line and not line.startswith("#"):
                if "==" in line:
                    name, version=line.split("==", 1)
                    packages.append((name.strip(), version.strip()))
                else:
                    packages.append((line, "unknown"))
    
    try:
        result=subprocess.run(
            ["pip", "freeze"],
            capture_output=True,
            text=True,
            cwd=context_dir,
            timeout=10
        )
        if result.returncode==0:
            for line in result.stdout.splitlines():
                if "==" in line:
                    name, version=line.split("==", 1)
                    if not any(p[0]==name for p in packages):
                        packages.append((name.strip(), version.strip()))
    except: pass
    
    return sorted(packages)

def _generate_doc_id() -> str:
    """Generate unique document ID."""
    return hashlib.sha256(datetime.now(timezone.utc).isoformat().encode()).hexdigest()[:16]
