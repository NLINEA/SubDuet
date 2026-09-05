#!/usr/bin/env python3
"""Fail closed when SubDuet's installed runtime graph violates its license policy."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import uuid
from collections import deque
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", default="subduet")
    parser.add_argument("--policy", type=Path, default=Path("license-policy.toml"))
    parser.add_argument("--sbom", type=Path)
    return parser.parse_args()


def _runtime_graph(root_name: str) -> list[metadata.Distribution]:
    queue = deque([(canonicalize_name(root_name), frozenset[str]())])
    visited: set[tuple[str, frozenset[str]]] = set()
    graph: dict[str, metadata.Distribution] = {}
    while queue:
        name, active_extras = queue.popleft()
        key = (name, active_extras)
        if key in visited:
            continue
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"required distribution is not installed: {name}") from exc
        visited.add(key)
        graph[name] = distribution
        for requirement_text in distribution.requires or ():
            requirement = Requirement(requirement_text)
            marker_extras = {"", *active_extras}
            if requirement.marker is not None and not any(
                requirement.marker.evaluate({"extra": extra}) for extra in marker_extras
            ):
                continue
            queue.append((canonicalize_name(requirement.name), frozenset(requirement.extras)))
    return sorted(graph.values(), key=lambda item: canonicalize_name(item.metadata["Name"]))


def _declared_license(distribution: metadata.Distribution, overrides: dict[str, str]) -> str:
    name = canonicalize_name(distribution.metadata["Name"])
    if name in overrides:
        return overrides[name]
    expression = distribution.metadata.get("License-Expression", "").strip()
    if expression:
        return expression
    declared = distribution.metadata.get("License", "").strip()
    if declared and len(declared) < 160:
        return declared
    classifiers = distribution.metadata.get_all("Classifier") or ()
    license_classifiers = [
        classifier.removeprefix("License :: OSI Approved :: ").removesuffix(" License")
        for classifier in classifiers
        if classifier.startswith("License :: OSI Approved :: ")
    ]
    if license_classifiers:
        return " OR ".join(license_classifiers)
    if "Permission is hereby granted, free of charge" in declared:
        return "MIT"
    return "UNKNOWN"


def _license_ids(value: str, allowed: set[str]) -> set[str]:
    compact = value.casefold().replace("license", "").strip()
    aliases = {
        "apache software 2.0": "Apache-2.0",
        "apache 2.0": "Apache-2.0",
        "bsd": "BSD-3-Clause",
        "bsd 3-clause": "BSD-3-Clause",
        "isc": "ISC",
        "mit": "MIT",
        "mit-cmu": "MIT",
        "mozilla public 2.0": "MPL-2.0",
        "python software foundation": "PSF-2.0",
        "the unlicense (unlicense)": "Unlicense",
    }
    if compact in aliases:
        return {aliases[compact]}
    return {
        identifier
        for identifier in allowed
        if re.search(rf"(?<![A-Za-z0-9.-]){re.escape(identifier)}(?![A-Za-z0-9.-])", value)
    }


def _is_fully_recognized(value: str, identifiers: set[str]) -> bool:
    remaining = value
    for identifier in sorted(identifiers, key=len, reverse=True):
        remaining = re.sub(
            rf"(?<![A-Za-z0-9.-]){re.escape(identifier)}(?![A-Za-z0-9.-])",
            "",
            remaining,
        )
    remaining = re.sub(r"\b(?:AND|OR|WITH)\b|[()+]", "", remaining, flags=re.IGNORECASE)
    return not remaining.strip()


def _component(distribution: metadata.Distribution, declared_license: str) -> dict[str, Any]:
    name = distribution.metadata["Name"]
    version = distribution.version
    return {
        "type": "library",
        "name": name,
        "version": version,
        "purl": f"pkg:pypi/{canonicalize_name(name)}@{version}",
        "licenses": [{"expression": declared_license}],
    }


def _write_sbom(path: Path, components: list[dict[str, Any]]) -> None:
    identity = ";".join(component["purl"] for component in components)
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
        "version": 1,
        "metadata": {
            "component": next(
                item for item in components if canonicalize_name(item["name"]) == "subduet"
            )
        },
        "components": components,
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    arguments = _arguments()
    policy = tomllib.loads(arguments.policy.read_text(encoding="utf-8"))
    allowed = set(policy["allowed_licenses"])
    denied = tuple(policy["denied_license_fragments"])
    blocked = {canonicalize_name(name) for name in policy["blocked_packages"]}
    overrides = {
        canonicalize_name(name): value
        for name, value in policy.get("license_overrides", {}).items()
    }
    reviewed_exceptions = {
        canonicalize_name(name): value
        for name, value in policy.get("reviewed_license_exceptions", {}).items()
    }

    failures: list[str] = []
    components: list[dict[str, Any]] = []
    for distribution in _runtime_graph(arguments.distribution):
        name = canonicalize_name(distribution.metadata["Name"])
        declared = _declared_license(distribution, overrides)
        components.append(_component(distribution, declared))
        if name in blocked:
            failures.append(f"{name} is explicitly blocked")
            continue
        if any(fragment.casefold() in declared.casefold() for fragment in denied):
            failures.append(f"{name} declares denied license: {declared}")
            continue
        if name in reviewed_exceptions:
            if declared != reviewed_exceptions[name]:
                failures.append(
                    f"{name} license changed from reviewed exception "
                    f"{reviewed_exceptions[name]} to {declared}"
                )
            continue
        identifiers = _license_ids(declared, allowed)
        if not identifiers or not identifiers <= allowed or not _is_fully_recognized(
            declared, identifiers
        ):
            failures.append(f"{name} has unknown or unapproved license metadata: {declared}")

    for component in components:
        expression = component["licenses"][0]["expression"]
        print(f"{component['name']} {component['version']}: {expression}")
    if failures:
        print("\nLicense policy violations:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    if arguments.sbom:
        _write_sbom(arguments.sbom, components)
        print(f"Wrote CycloneDX SBOM to {arguments.sbom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
