"""A small, derived graph of reviewed business definitions; never a rule publisher."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from importlib.resources import files
from typing import Any

from .semantics import load_metric_definitions
from .organization import OrganizationCatalog, LEVELS
from .text_encoding import dumps_json


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


class KnowledgeGraph:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_nodes (
                node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_edges (
                source TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL,
                evidence_json TEXT NOT NULL, PRIMARY KEY(source, relation, target)
            );
            CREATE TABLE IF NOT EXISTS knowledge_versions (name TEXT PRIMARY KEY, version TEXT NOT NULL);
        """)
        payload = files("analytics_agent").joinpath("data", "business_glossary.json").read_text(encoding="utf-8")
        self.organization = OrganizationCatalog()
        payload += "knowledge-v2" + dumps_json(self.organization.payload, ensure_ascii=False)
        self.version = hashlib.sha256(payload.encode()).hexdigest()[:16]
        existing = connection.execute("SELECT version FROM knowledge_versions WHERE name = 'glossary'").fetchone()
        if not existing or existing[0] != self.version:
            self._seed()

    def _seed(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM knowledge_edges")
            self.connection.execute("DELETE FROM knowledge_nodes")
            for key, rule in load_metric_definitions().items():
                concept = f"metric:{key}"
                table = f"table:{rule['table_id']}"
                evidence = {"version": self.version, "source": rule.get("source", "business_glossary.json"),
                            "status": "validated", "validated_by": rule.get("validated_by", "reviewed_glossary")}
                self._node(concept, "metric", rule.get("term", key), {**rule, "key": key})
                self._node(table, "table", rule["table_id"], {})
                self._edge(concept, "uses_table", table, evidence)
                if rule.get("routine_id"):
                    routine = "routine:" + rule["routine_id"]
                    self._node(routine, "routine", rule["routine_id"], {"signature": "verified_at_runtime", "inclusive_analysis_end": True})
                    self._edge(concept, "uses_routine", routine, evidence)
                for role in ("numerator", "denominator"):
                    if rule.get(role):
                        self._edge(concept, "uses_" + role, "metric:" + rule[role], evidence)
                if rule.get("kind") == "ratio":
                    continue  # Its numerator and denominator own the physical mappings.
                field_relations = ({"fecha_iniciovig": "uses_argument", "fecha_finvig": "uses_argument", "vrprima": "uses_argument"}
                                   if rule.get("kind") == "routine" else
                                   {rule["value_field"]: "aggregates", rule["date_field"]: "uses_date"})
                for name in rule.get("dimensions", {}):
                    field_relations.setdefault(name, "groups_by")
                for field, relation in field_relations.items():
                    node = f"column:{rule['table_id']}.{field}"
                    self._node(node, "column", field, {"table": rule["table_id"]})
                    self._edge(node, "belongs_to", table, evidence)
                    self._edge(concept, relation, node, {**evidence, "aggregation": rule["aggregation"]})
                for item in rule.get("required_filters", []):
                    node = f"filter:{key}:{item['field']}"
                    self._node(node, "filter", f"{item['field']}={item.get('value')}", dict(item))
                    self._edge(concept, "requires_filter", node, evidence)
            evidence = {"version": self.version, "source": self.organization.payload["source"],
                        "validated_by": self.organization.payload["reviewed_by"], "temporal_scope": "current_catalog"}
            for row in self.organization.rows:
                local = f"local_code:{row['cod_subprofit_loc']}"
                self._node(local, "local_code", str(row["cod_subprofit_loc"]), dict(row))
                for level in LEVELS:
                    node = f"organization:{level}:{row[level]}"
                    self._node(node, "organization", row[level], {"level": level})
                    self._edge(local, "belongs_to_" + level, node, evidence)
                    self._edge(node, "contains_local_code", local, evidence)
            for link in self.organization.payload["subprofit_links"]:
                node = f"subprofit:{link['cod_subprofit']}"
                self._node(node, "subprofit", str(link["cod_subprofit"]), {"unique": False})
                self._edge(node, "maps_to", f"local_code:{link['cod_subprofit_loc']}", evidence)
            self.connection.execute("INSERT OR REPLACE INTO knowledge_versions VALUES ('glossary', ?)", (self.version,))

    def _node(self, key: str, kind: str, label: str, payload: dict[str, Any]) -> None:
        self.connection.execute("INSERT OR REPLACE INTO knowledge_nodes VALUES (?, ?, ?, ?)", (key, kind, label, dumps_json(payload, ensure_ascii=False)))

    def _edge(self, source: str, relation: str, target: str, evidence: dict[str, Any]) -> None:
        self.connection.execute("INSERT OR REPLACE INTO knowledge_edges VALUES (?, ?, ?, ?)", (source, relation, target, dumps_json(evidence, ensure_ascii=False)))

    def search(self, query: str, *, limit: int = 4) -> dict[str, Any]:
        tokens = {token for token in re.findall(r"\w+", normalized(query)) if len(token) > 2}
        ranked = []
        for row in self.connection.execute("SELECT * FROM knowledge_nodes WHERE kind = 'metric'"):
            rule = json.loads(row["payload_json"])
            words = normalized(" ".join([rule["key"], row["label"], *rule.get("aliases", []), rule.get("definition", "")]))
            score = sum(token in words for token in tokens)
            if score:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], item[1]["node_id"]))
        matches = []
        for _, row in ranked[:max(1, min(limit, 8))]:
            edges = []
            for edge in self.connection.execute("""SELECT e.relation, e.evidence_json, n.kind, n.label, n.payload_json
                FROM knowledge_edges e JOIN knowledge_nodes n ON n.node_id = e.target
                WHERE e.source = ? ORDER BY e.relation, n.label""", (row["node_id"],)):
                if edge["relation"] == "groups_by":
                    continue  # Dimensions remain in the compiler; keep model context compact.
                edges.append({"relation": edge["relation"], "kind": edge["kind"], "label": edge["label"],
                              "details": json.loads(edge["payload_json"]), "evidence": json.loads(edge["evidence_json"])})
            matches.append({"concept": row["label"], "metric_key": row["node_id"].split(":", 1)[1], "relationships": edges})
        return {"version": self.version, "matches": matches, "organization": self.organization.search(query),
                "note": "Índice derivado del glosario revisado; las propuestas personales no son fórmulas compartidas."}
