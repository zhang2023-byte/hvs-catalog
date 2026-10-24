"""Explicit SIMBAD acquisition, frozen replay, and nearest-candidate selection."""

import json
import math
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from ..records.catalog import (
    Association,
    SimbadCandidate,
    SimbadQuery,
    SimbadResponse,
    SimbadSnapshot,
    SkyPosition,
)
from ..records.common import sha256_of
from .identity import coordinate, gaia_conflicts, keys, names, separation, sky
from .inputs import json_bytes


def query_key(query):
    return sha256_of(query.model_dump(mode="json"))


class FrozenSimbad:
    """Read saved responses only. Missing entries never trigger network activity."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        self.used = {}

    def read(self, query):
        key = query_key(query)
        path = self.root / (key + ".json") if self.root else None
        if path is None or not path.exists():
            return None
        payload = json.loads(path.read_bytes())
        response = SimbadResponse.model_validate(payload["response"])
        if (
            response.query != query
            or sha256_of(response.model_dump(mode="json")) != payload["sha256"]
        ):
            raise ValueError("Frozen SIMBAD response binding is invalid.")
        self.used[key] = payload["sha256"]
        return response


def queries_for(stars):
    identifiers = sorted({key for _, star in stars for key in keys(star)})
    queries = [
        SimbadQuery(method="identifier", identifier=name) for name in identifiers
    ]
    positions = {}
    for _, star in stars:
        pos = coordinate(star)
        if pos:
            # Evidence remains on the association; a reusable query contains only the position.
            pos = pos.model_copy(update={"evidence": []})
            query = SimbadQuery(method="coordinates", position=pos)
            positions[query_key(query)] = query
    return queries + [positions[k] for k in sorted(positions)]


def resolve_simbad(stars, frozen):
    queries = queries_for(stars)
    history, candidates, warnings = [], [], []
    all_names = [name for _, star in stars for name in names(star)]
    any_failure = False
    if gaia_conflicts(all_names):
        return SimbadSnapshot(
            status="conflict",
            reason="Reported Gaia identities conflict within a release.",
        )

    def read(query):
        nonlocal any_failure
        response = frozen.read(query)
        history.append(
            {
                "query": query.model_dump(mode="json"),
                "response_sha256": sha256_of(response.model_dump(mode="json"))
                if response
                else None,
                "queried_at": response.queried_at if response else None,
                "client_version": response.client_version if response else None,
                "service_version": response.service_version if response else None,
                "error": response.error if response else None,
            }
        )
        if response and response.error:
            any_failure = True
        return response

    def result(status, method=None, selected=None, reason=None):
        return SimbadSnapshot(
            status=status,
            method=method,
            selected=selected,
            queries=history,
            candidates=candidates,
            reason=reason,
            warnings=warnings,
        )

    selected = {}
    for query in [q for q in queries if q.method == "identifier"]:
        response = read(query)
        if not response or response.error:
            continue
        for item in response.candidates:
            candidates.append(
                {
                    "main_id": item.main_id,
                    "method": "identifier",
                    "identifier": query.identifier,
                    "candidate": item.model_dump(mode="json"),
                }
            )
            selected[item.main_id] = item
    if len(selected) > 1:
        return result(
            "conflict", reason="Reliable identifiers resolve to different objects."
        )
    if any_failure:
        return result("failed", reason="An identifier query failed.")
    if selected:
        item = next(iter(selected.values()))
        if gaia_conflicts(
            [
                *all_names,
                *(
                    name
                    for c in candidates
                    for name in [c["candidate"]["main_id"], *c["candidate"]["aliases"]]
                ),
            ]
        ):
            return result(
                "conflict", reason="SIMBAD aliases conflict with reported identifiers."
            )
        if any(q["response_sha256"] is None for q in history):
            warnings.append("Some identifier queries have no frozen response.")
        return result("matched", "identifier", item)

    coordinate_choices = {}
    ambiguous = False
    for query in [q for q in queries if q.method == "coordinates"]:
        response = read(query)
        if not response or response.error:
            continue
        ranked = []
        for item in response.candidates:
            if item.position is None:
                continue
            distance = separation(query.position, item.position)
            if distance <= 5.0 + 1e-9:
                ranked.append((distance, item.main_id, item))
                candidates.append(
                    {
                        "main_id": item.main_id,
                        "method": "coordinates",
                        "separation_arcsec": distance,
                        "candidate": item.model_dump(mode="json"),
                        "query": query.model_dump(mode="json"),
                    }
                )
        ranked.sort(key=lambda item: (item[0], item[1]))
        if len(ranked) > 1:
            warnings.append(
                "Multiple SIMBAD candidates; the nearest candidate was considered."
            )
            if math.isclose(ranked[0][0], ranked[1][0], abs_tol=1e-9, rel_tol=0):
                ambiguous = True
                continue
        if ranked:
            item = ranked[0][2]
            coordinate_choices[item.main_id] = item
    if any_failure:
        return result("failed", reason="A coordinate query failed.")
    if ambiguous or len(coordinate_choices) > 1:
        return result(
            "ambiguous",
            reason="Coordinates do not select one unambiguous nearest object.",
        )
    if coordinate_choices:
        item = next(iter(coordinate_choices.values()))
        if gaia_conflicts([*all_names, *item.aliases, item.main_id]):
            return result(
                "conflict",
                reason="The nearest candidate conflicts with reported identifiers.",
            )
        warnings.append(
            "SIMBAD association is based on coordinates only; no epoch propagation."
        )
        return result("matched", "coordinates", item)
    if not queries or any(q["response_sha256"] is None for q in history):
        return result(
            "not_queried",
            reason="No usable query or a required frozen response is missing.",
        )
    return result(
        "not_found", reason="No candidate matched the requested identifiers or radius."
    )


def selected_candidates(snapshot):
    """Include every frozen representation of the selected external identity."""
    if snapshot.selected is None:
        return []
    values = [
        snapshot.selected.model_dump(mode="json"),
        *(
            c["candidate"]
            for c in snapshot.candidates
            if c.get("candidate", {}).get("main_id") == snapshot.selected.main_id
        ),
    ]
    return list({sha256_of(c): c for c in values}.values())


def previous_identity_conflicts(edges, members):
    """Keep rejected fixed identity evidence on the affected current source group."""
    conflicts = []
    for edge in edges:
        if (
            edge.accepted
            or edge.left not in members
            or edge.method != "simbad"
            or not edge.details.get("matched_identifiers")
        ):
            continue
        evidence = edge.details["simbad_evidence"]
        reason = edge.details["reason"]
        conflicts.append(
            SimbadSnapshot(
                status="conflict",
                reason=reason,
                queries=[q for item in evidence for q in item["queries"]],
                candidates=[
                    {
                        "main_id": item["candidate"]["main_id"],
                        "method": "identifier",
                        "rejected_candidate": item["candidate"],
                        "source_node": edge.left,
                        "reason": reason,
                    }
                    for item in evidence
                ],
            )
        )
    return conflicts


def combine_snapshots(stars, snapshots):
    """Preserve every frozen input and reject incompatible external identities."""
    if not snapshots:
        return SimbadSnapshot(status="not_queried")
    unresolved = next(
        (
            s
            for status in ("conflict", "ambiguous", "failed")
            for s in snapshots
            if s.status == status
        ),
        None,
    )
    chosen = unresolved or next(
        (s for s in snapshots if s.status == "matched" and s.method == "identifier"),
        next((s for s in snapshots if s.status == "matched"), snapshots[0]),
    )
    result = chosen.model_copy(deep=True)
    for field in ("queries", "candidates"):
        setattr(
            result,
            field,
            list(
                {sha256_of(v): v for s in snapshots for v in getattr(s, field)}.values()
            ),
        )
    result.warnings = list(dict.fromkeys(w for s in snapshots for w in s.warnings))
    selected = [s for s in snapshots if s.selected]
    reported = [name for _, star in stars for name in names(star)]
    external = [
        name
        for s in selected
        for c in selected_candidates(s)
        for name in [c["main_id"], *c["aliases"]]
    ]
    reason = None
    if len({s.selected.main_id for s in selected}) > 1:
        reason = "Frozen SIMBAD identities resolve to different objects."
    elif gaia_conflicts([*reported, *external]):
        reason = "Previous SIMBAD identity conflicts with new evidence."
    if reason or unresolved:
        result = result.model_copy(
            update={
                "status": "conflict" if reason else unresolved.status,
                "method": None,
                "selected": None,
                "reason": reason or unresolved.reason,
            }
        )
        result.candidates.extend(
            {
                "main_id": s.selected.main_id,
                "method": s.method,
                "rejected_candidate": s.selected.model_dump(mode="json"),
                "reported_identifiers": reported,
                "reason": result.reason,
            }
            for s in selected
        )
    return result


def coordinate_associations(stars, snapshot, origins):
    """Bind each adopted cone-search result to its actual paper coordinate evidence."""
    if snapshot.status != "matched" or snapshot.method != "coordinates":
        return []
    edges = []
    for node, star in stars:
        pos = coordinate(star)
        if pos is None:
            continue
        query = SimbadQuery(
            method="coordinates", position=pos.model_copy(update={"evidence": []})
        ).model_dump(mode="json")
        history = next((q for q in snapshot.queries if q["query"] == query), None)
        candidates = [c for c in snapshot.candidates if c.get("query") == query]
        chosen = next(
            (c for c in candidates if c["main_id"] == snapshot.selected.main_id), None
        )
        if chosen is None or history is None or not history.get("response_sha256"):
            continue
        edges.append(
            Association(
                left=node,
                right="simbad:" + snapshot.selected.main_id,
                method="coordinates",
                accepted=True,
                origins={node: origins[node]},
                details={
                    "left_position": pos.model_dump(mode="json"),
                    "right_position": chosen["candidate"]["position"],
                    "separation_arcsec": chosen["separation_arcsec"],
                    "threshold_arcsec": 5.0,
                    "epoch_propagation": False,
                    "selection": "nearest",
                    "query": history,
                    "candidates": candidates,
                },
            )
        )
    return edges


class AstroquerySimbad:
    """Query only SIMBAD basic, identifier and photometry tables through Astroquery."""

    def __init__(self, client=None):
        if client is None:
            from astroquery.simbad import Simbad

            client = Simbad()
        self.client = client

    def query(self, query):
        import numpy as np
        from astroquery.exceptions import RemoteServiceError
        from pyvo.dal.exceptions import DALAccessError
        from requests.exceptions import RequestException

        captured = []
        if hasattr(self.client, "clear_cache"):
            self.client.clear_cache()

        def capture(response, *args, **kwargs):
            chunks = []
            captured.append((response.encoding or "utf-8", chunks))
            original_read = response.raw.read

            def record_read(*read_args, **read_kwargs):
                data = original_read(*read_args, **read_kwargs)
                chunks.append(data)
                return data

            response.raw.read = record_read

        session = getattr(self.client, "_session", None)
        if session is not None:
            session.hooks.setdefault("response", []).append(capture)

        def value(item):
            if np.ma.is_masked(item):
                return None
            if hasattr(item, "item"):
                item = item.item()
            if isinstance(item, bytes):
                item = item.decode()
            if isinstance(item, float) and not math.isfinite(item):
                return None
            return item

        def fields(table, row):
            return {
                name: {
                    "value": value(row[name]),
                    **(
                        {"unit": str(table[name].unit)}
                        if table[name].unit is not None
                        else {}
                    ),
                }
                for name in table.colnames
                if value(row[name]) is not None
            }

        candidates = []
        error = None
        try:
            if query.method == "identifier":
                identifier = query.identifier.replace("'", "''")
                where = (
                    "b.oid IN (SELECT oidref FROM ident WHERE id = '"
                    + identifier
                    + "')"
                )
            else:
                pos = sky(query.position).icrs
                where = (
                    "1=CONTAINS(POINT('ICRS',b.ra,b.dec),CIRCLE('ICRS',"
                    f"{pos.ra.deg:.16g},{pos.dec.deg:.16g},{5 / 3600:.16g}))"
                )
            # maxrec is bounded; hitting the bound is a failure, never a truncated match.
            table = self.client.query_tap(
                "SELECT b.*, i.ids FROM basic AS b LEFT JOIN ids AS i ON b.oid=i.oidref WHERE "
                + where,
                maxrec=10000,
            )
            if len(table) >= 10000:
                raise ValueError("SIMBAD candidate response reached its row limit.")
            for row in table:
                data = fields(table, row)
                main_id = str(value(row["main_id"]))
                aliases = str(value(row["ids"]) or "").split("|")
                oid = int(row["oid"])
                flux = self.client.query_tap(
                    f"SELECT * FROM flux WHERE oidref={oid}", maxrec=10000
                )
                if len(flux) >= 10000:
                    raise ValueError(
                        "SIMBAD photometry response reached its row limit."
                    )
                photometry = []
                for flux_row in flux:
                    band_data = fields(flux, flux_row)
                    if "filter" not in band_data:
                        raise ValueError("Returned photometry has no band.")
                    photometry.append(
                        {"band": band_data["filter"]["value"], "fields": band_data}
                    )
                pos = None
                if "ra" in data and "dec" in data:
                    pos = SkyPosition(
                        ra_deg=float(data["ra"]["value"]),
                        dec_deg=float(data["dec"]["value"]),
                        frame="icrs",
                    )
                candidates.append(
                    SimbadCandidate(
                        main_id=main_id,
                        aliases=sorted({a for a in aliases if a}),
                        position=pos,
                        fields={
                            k: v for k, v in data.items() if k not in {"ids", "oid"}
                        },
                        photometry=photometry,
                    )
                )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RequestException,
            DALAccessError,
            RemoteServiceError,
        ) as exc:
            error = type(exc).__name__ + ": " + str(exc)
            candidates = []
        finally:
            if session is not None:
                session.hooks["response"].remove(capture)
        return SimbadResponse(
            query=query,
            queried_at=datetime.now(UTC).isoformat(),
            client_version=version("astroquery"),
            candidates=candidates,
            raw_response=json.dumps(
                [
                    b"".join(chunks).decode(encoding, errors="replace")
                    for encoding, chunks in captured
                ],
                ensure_ascii=False,
            ),
            error=error,
        )


def collect_queries(queries, destination, client):
    """Execute only an explicit query list; existing frozen replies are reused."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    frozen = FrozenSimbad(destination)
    results = {}
    for query in queries:
        query = SimbadQuery.model_validate(query)
        key = query_key(query)
        response = frozen.read(query)
        if response is None:
            response = client.query(query)
            if response.query != query:
                raise ValueError("Service returned a response for another query.")
            payload = {
                "sha256": sha256_of(response.model_dump(mode="json")),
                "response": response.model_dump(mode="json"),
            }
            # Atomic publication prevents partially written responses being reused.
            from ..workspace.layout import _write_file_published

            _write_file_published(destination / (key + ".json"), json_bytes(payload))
        results[key] = "failed" if response.error else "saved"
    return results
