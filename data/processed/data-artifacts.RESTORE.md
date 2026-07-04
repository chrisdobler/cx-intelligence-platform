Derived data artifact bundle: data/processed/data-artifacts.tgz
Checksum: data/processed/data-artifacts.tgz.sha256
Import derived AI data: app import-derived data/processed/data-artifacts.tgz
Inspect: tar -tzf data/processed/data-artifacts.tgz | head
Extract: mkdir -p /tmp/cxintel-artifacts && tar -xzf data/processed/data-artifacts.tgz -C /tmp/cxintel-artifacts
