"""Pure parsing of the TotalEnergies payloads (synthetic data)."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.totalenergies_pro.api import (
    parse_accounts,
    parse_consumptions,
    parse_invoices,
    parse_sites,
    windows,
)
from custom_components.totalenergies_pro.api.models import parse_date, parse_number

from .conftest import INVOICE_1, PCE, PRM, invoice, line


def test_numbers_and_dates() -> None:
    assert parse_number("1 234,56") == 1234.56
    assert parse_number("1 234,5") == 1234.5
    assert parse_number("-30") == -30.0
    assert parse_number(12) == 12.0
    for bad in (None, "", "n/a", True, [1]):
        assert parse_number(bad) is None
    assert parse_date("2026-03-01T00:00:00") == date(2026, 3, 1)
    for empty in ("0000-00-00", "9999-12-31", "", None, "31/12/2026", 20260101):
        assert parse_date(empty) is None


def test_windows() -> None:
    assert windows(date(2026, 1, 1), date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]
    assert windows(date(2024, 2, 29), date(2026, 3, 1)) == [
        (date(2024, 2, 29), date(2025, 2, 27)),
        (date(2025, 2, 28), date(2026, 2, 27)),
        (date(2026, 2, 28), date(2026, 3, 1)),
    ]
    assert windows(date(2026, 1, 31), date(2026, 4, 30), months=1)[0] == (
        date(2026, 1, 31),
        date(2026, 2, 27),
    )
    with pytest.raises(ValueError):
        windows(date(2026, 2, 1), date(2026, 1, 1))


def test_accounts_wrapped_and_flat() -> None:
    accounts = parse_accounts(
        {
            "comptes": [
                {"compte": {"numero": "200000000001", "nom": "A", "segment": "C4"}},
                {"numero": 200000000002, "nom": " ", "modeEnvoiFacture": "EMAIL"},
                "junk",
            ]
        }
    )
    assert [(a.number, a.name, a.segment, a.invoice_delivery) for a in accounts] == [
        ("200000000001", "A", "C4", None),
        ("200000000002", None, None, "EMAIL"),
        (None, None, None, None),
    ]
    assert parse_accounts([]) == []
    assert parse_accounts({"comptes": None}) == []


def test_sites_and_contracts() -> None:
    sites = parse_sites(
        {
            "sites": [
                {
                    "site": {
                        "pod": PRM,
                        "energie": "01",
                        "segment": "C4",
                        "designation": "SITE SYNTH",
                        "isCommunicant": True,
                        "idCompte": "300000000001",
                        "situationContractuelle": {
                            "numeroContrat": "400000000001",
                            "statutContrat": "50",
                            "dateDebutContrat": "2025-01-01",
                            "dateResiliation": "9999-12-31",
                            "isActif": True,
                            "fta": "BTSUPCU4",
                            "optionTarifaire": "CU4",
                            "puissanceHeuresPleinesHiver": 42,
                            "puissanceHeuresCreusesHiver": "42",
                            "puissancePointe": None,
                            "poucentageEnergieVerte": "0",
                        },
                        "situationComptage": {"matriculeCompteur": "M1", "frequenceReleve": "M"},
                    }
                },
                {
                    "pod": PCE,
                    "energie": "02",
                    "situationContractuelle": {
                        "statutContrat": "99",
                        "isResilie": True,
                        "dateResiliation": "2024-12-30",
                    },
                },
                {"pod": "x", "situationContractuelle": {"isEnCoursDeResiliation": True}},
                {"pod": "y", "situationContractuelle": {"statutContrat": "99", "isActif": True}},
                {"pod": "z", "situationContractuelle": "junk", "situationComptage": None},
            ]
        }
    )
    elec, gas, leaving, unknown_code, junk = sites
    assert elec.energy == "electricity"
    assert elec.contract.status == "active"
    assert elec.contract.start == date(2025, 1, 1)
    assert elec.contract.termination is None
    assert elec.contract.subscribed_power_kva == {"hph": 42.0, "hch": 42.0}
    assert elec.contract.green_energy_pct == 0.0
    assert (elec.meter, elec.reading_frequency, elec.communicating) == ("M1", "M", True)
    assert gas.energy == "gas"
    assert (gas.contract.status, gas.contract.termination) == ("terminated", date(2024, 12, 30))
    assert not gas.contract.active
    assert leaving.contract.status == "terminating"
    assert unknown_code.contract.status == "active"
    assert (junk.contract.status, junk.energy, junk.meter) == ("unknown", None, None)


def test_invoices() -> None:
    raw = invoice(INVOICE_1, "2026-02-10")
    cancelled = invoice("100000000009", "2026-01-10")
    cancelled["facture"]["numeroFactureAnnulation"] = "100000000010"
    cancelled["facture"]["dateImpressionAnnulation"] = "2026-02-01"
    flat = {"numero": "100000000011", "secteurActivite": "03", "nbPOD": "deux"}

    invoices, heavy = parse_invoices({"factures": [raw, cancelled, flat], "isHeavyLoad": False})
    assert not heavy
    first, second, third = invoices
    assert first.number == INVOICE_1
    assert first.date == date(2026, 2, 10)
    assert first.energy == "electricity"
    assert (first.subtotal_excl_tax, first.taxes_excl_vat) == (80.0, 20.42)
    assert (first.total_excl_vat, first.vat, first.total_incl_vat) == (100.42, 20.08, 120.5)
    assert first.points == 1
    assert first.cancellation_invoice is None
    assert first.cancellation_printed_on is None
    assert not first.cancelled
    assert second.cancelled
    assert second.cancellation_printed_on == date(2026, 2, 1)
    assert (third.energy, third.points, third.date) == (None, None, None)
    assert parse_invoices("junk") == ([], False)


def test_consumption_lines() -> None:
    gas = line(INVOICE_1, "2026-01-01", "2026-01-31", "", "5000", "300")
    gas.update({"pod": PCE, "energie": "02", "libelleCalFou": None})
    lines, partial = parse_consumptions(
        {"consumptions": [gas, line(INVOICE_1, "2026-01-01", "2026-01-31", "HPH", "1", "0,2")]}
    )
    assert not partial
    g, e = lines
    assert (g.point, g.energy, g.time_class, g.supplier_calendar) == (PCE, "gas", None, "CAL1")
    assert (g.start, g.end, g.energy_kwh, g.amount_excl_tax) == (
        date(2026, 1, 1),
        date(2026, 1, 31),
        5000.0,
        300.0,
    )
    assert e.key == (
        INVOICE_1,
        PRM,
        "2026-01-01",
        "2026-01-31",
        "HPH",
        "1",
        "Calendrier synthétique",
    )
    assert parse_consumptions({"details": {"hasError": True}}) == ([], True)
