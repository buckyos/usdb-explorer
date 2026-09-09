"""Small Trivy reports for policy and evidence tests, never production scan evidence."""


def trivy_report(reference, revision, name="gateway"):
    """Include identity, platform, binary coverage and a known unresolved synthetic finding."""
    return {"SchemaVersion": 2, "ArtifactType": "container_image", "ArtifactName": reference,
            "Metadata": {"RepoDigests": [reference], "ImageConfig": {"os": "linux", "architecture": "amd64",
                "config": {"Labels": {"org.opencontainers.image.revision": revision}}}},
            "Results": [{"Target": "gateway" if name == "gateway" else "Alpine Linux",
                "Class": "lang-pkgs" if name == "gateway" else "os-pkgs",
                "Type": "gobinary" if name == "gateway" else "alpine",
                "Vulnerabilities": [{"VulnerabilityID": "CVE-2099-0001", "PkgName": "fixture-package",
                    "InstalledVersion": "1.0", "FixedVersion": "1.1", "Severity": "HIGH"}]}]}


def trivy_sarif():
    return {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Trivy"}},
            "results": [{"ruleId": "CVE-2099-0001", "message": {"text": "Synthetic High finding"}}]}]}
