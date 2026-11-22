"""Invoice PDF reading: synthetic text laid out like a TotalEnergies invoice, and a tiny PDF built
here (no real invoice in the repository)."""

# ruff: noqa: E501 — the synthetic invoices keep the long rows of the real layout

from __future__ import annotations

from datetime import date

import pytest

from custom_components.totalenergies_pro.invoice_pdf import (
    InvoiceDetail,
    PdfSummary,
    number,
    parse_text,
    pdf_text,
    read_invoice,
    summarize_details,
    summarize_invoice,
)

ELEC = """
                                   Date de facture : 10/01/26
  Option tarifaire : Courte Utilisation          Segment : C4
  Raccordement : BT > 36 kVA

  Energie active (HT)                                                        1 234,56 €
  Heures Pleines Hiver        25/12/25  31/12/25    700 kWh    0,12000 €/kWh     84,00 €   20 %
  Heures Pleines Hiver        01/01/26  24/01/26    300 kWh    0,10000 €/kWh     30,00 €   20 %
  Heures Creuses Hiver        25/12/25  24/01/26    500 kWh    0,08000 €/kWh     40,00 €   20 %
  Mécanisme de capacité Heures Pleines Hiver  25/12/25  24/01/26  1 000 kWh  0,00500 €/kWh  5,00 €  20 %
  Certificat d'économie d'énergie (coef : 0,478 )  25/12/25  24/01/26  717 kWh  0,00800 €/kWh  5,74 €  20 %
  Abonnement (HT)                                                               80,00 €
  Abonnement fournisseur Echu   25/12/25  24/01/26                420,00 €/an    35,00 €   20 %
  Abonnement fournisseur Reprise  25/12/25  31/12/25              420,00 €/an    -8,00 €   20 %
  Transport et acheminement (HT)                                               200,00 €
  Composante de soutirage variable Heures Pleines Hiver  25/12/25  24/01/26  1 000 kWh  0,02000 €/kWh  20,00 €  20 %
  Composante de soutirage fixe - Echu  25/12/25  24/01/26  1 200 kW.j  0,05000 €/kW.j  60,00 €  20 %
  Contributions et taxes liées (HT)                                             60,00 €
  Accise sur l'électricité    25/12/25  24/01/26   1 500 kWh    0,02500 €/kWh     37,50 €   20 %
  Contribution Tarifaire d'Acheminement (CTA)  25/12/25  24/01/26                  20,00 €   20 %

  Puissances souscrites (kVA)
  Heures Pleines Hiver   42
  Heures Creuses Hiver   42
  Puissance pondérée     42
  Historique de consommation
  Puissances mesurées
  Heures Pleines Hiver   45   3
  Heures Creuses Hiver   30   0
  Puissance pondérée     44
  Quantité
"""

GAS = """
  Date de facture : 10/10/25
  Offre Pro - Gaz Naturel            Tarif d'acheminement : T3     Zone Tarifaire : 2
  Profil : P012    Niveau Tarifaire Régional : 1
  Consommation Annuelle de Référence : 123 456 kWh
  Coef. de conversion (m3 à kWh) : 11,20
  Fourniture de gaz naturel (HT)                                             90,00 €
  Consommation                 02/09/25  01/10/25   2 000 kWh   0,04000 €/kWh    80,00 €   20 %
  CEE (coef : 0,500 | Prix €/kWh Cumac : 0,0100)  02/09/25  01/10/25  2 000 kWh  0,00500 €/kWh  10,00 €  20 %
  Acheminement (HT)                                                         20,00 €
  ATRD - Terme variable Distribution  02/09/25  01/10/25  2 000 kWh  0,01000 €/kWh   20,00 €   20 %
  ATRD - Terme fixe Distribution Echu  02/09/25  01/10/25          1000,00000 €/an    80,00 €   20 %
"""


# Layout of the invoices since November 2026: "Consommation <class>" labels, certificates per kWhc.
ELEC_2026_11 = """
  Date de facture : 10/11/26
  Energie active (HT)                                                          500,00 €
  Consommation Heures Pleines Eté   01/10/26  31/10/26   2 000 kWh   0,10000 €/kWh   200,00 €   20 %
  Consommation Heures Creuses Eté   01/10/26  31/10/26   1 000 kWh   0,05000 €/kWh    50,00 €   20 %
  Certificat d'économie d'énergie (coef : 0,7000 )  01/10/26  31/10/26  2 100,00 kWhc  0,01000 €/kWhc  21,00 €  20 %
  Certificat d'économie d'énergie précarité (coef : 0,7000 x 0,300 )  01/10/26  31/10/26  630,00 kWhc  0,01000 €/kWhc  6,30 €  20 %
  Abonnement fournisseur Echoir   01/11/26  30/11/26                420,00 €/an    35,00 €   20 %
  Composante de soutirage variable Heures Pleines Eté  01/10/26  31/10/26  2 000 kWh  0,02000 €/kWh  40,00 €  20 %
  Composante de comptage - Echoir  01/11/26  30/11/26  30 jour  0,80000 €/jour  24,00 €  20 %
  Accise sur l'électricité    01/10/26  31/10/26   3 000 kWh    0,02500 €/kWh     75,00 €   20 %
"""


def test_layout_of_november_2026() -> None:
    d = summarize_invoice(parse_text(ELEC_2026_11))
    # shared over 3000 kWh: certificates 27.30 €, excise 75 € → 0.0341
    assert d.marginal_price_by_class == {
        "hpe": pytest.approx(0.1 + 0.02 + 0.0341),
        "hce": pytest.approx(0.05 + 0.0341),
    }
    assert d.kwh == 3000.0
    # the period is the consumption one, not the subscription billed in advance
    assert (d.period_start, d.period_end) == (date(2026, 10, 1), date(2026, 10, 31))


def test_numbers() -> None:
    assert number("4 467") == 4467.0
    assert number("1301,40000") == 1301.4
    assert number("-30") == -30.0
    assert number("x") is None
    assert number(None) is None


def test_electricity_lines_powers_and_references() -> None:
    p = parse_text(ELEC)
    assert p.references == {
        "invoice_date": date(2026, 1, 10),
        "tariff_option": "Courte Utilisation",
        "segment": "C4",
        "connection": "BT > 36 kVA",
    }
    first = p.lines[0]
    assert (first.section, first.label, first.quantity, first.unit) == (
        "Energie active",
        "Heures Pleines Hiver",
        700.0,
        "kWh",
    )
    assert (first.unit_price, first.price_unit, first.amount_excl_tax) == (0.12, "kWh", 84.0)
    assert first.start == date(2025, 12, 25)
    reprise = next(x for x in p.lines if "Reprise" in x.label)
    assert (reprise.unit_price, reprise.price_unit, reprise.amount_excl_tax) == (420.0, "an", -8.0)
    cta = next(x for x in p.lines if "CTA" in x.label)
    assert (cta.unit_price, cta.amount_excl_tax) == (None, 20.0)
    assert p.subscribed_kva == {"hph": 42.0, "hch": 42.0, "weighted": 42.0}
    assert p.reached_kva == {"hph": 45.0, "hch": 30.0}
    assert p.overrun_hours == {"hph": 3.0, "hch": 0.0}


def test_marginal_price_per_class_with_two_periods() -> None:
    d = summarize_invoice(parse_text(ELEC))
    # HPH: 114 € for 1000 kWh (two periods, two prices), capacity 5 €, network 20 € → 0.139;
    # shared over 1500 kWh: certificates 5.74 €, excise 37.50 € → 0.02883.
    assert d.marginal_price_by_class == {
        "hph": pytest.approx(0.16783),
        "hch": pytest.approx(0.10883),
    }
    assert d.marginal_price == pytest.approx((0.16783 * 1000 + 0.10883 * 500) / 1500, abs=1e-5)
    assert d.kwh == 1500.0
    assert (d.period_start, d.period_end) == (date(2025, 12, 25), date(2026, 1, 24))
    assert d.invoice_date == date(2026, 1, 10)
    assert d.references == {
        "tariff_option": "Courte Utilisation",
        "segment": "C4",
        "connection": "BT > 36 kVA",
    }


def test_gas_references_and_price() -> None:
    d = summarize_invoice(parse_text(GAS))
    assert d.references == {
        "offer": "Offre Pro - Gaz Naturel",
        "network_tariff": "T3",
        "zone": "2",
        "profile": "P012",
        "regional_level": "1",
        "car_kwh": 123456.0,
        "conversion_factor": 11.2,
    }
    assert d.marginal_price_by_class == {}
    assert d.marginal_price == pytest.approx(0.055)
    assert d.reached_kva == {}


def test_no_lines() -> None:
    d = summarize_invoice(parse_text("nothing to read"))
    assert d == InvoiceDetail()


def test_roundtrip_and_odd_values() -> None:
    d = summarize_invoice(parse_text(ELEC))
    assert InvoiceDetail.from_dict(d.as_dict()) == d
    assert InvoiceDetail.from_dict(None) is None
    odd = InvoiceDetail.from_dict(
        {"invoice_date": "x", "kwh": True, "reached_kva": {"hph": "a"}, "references": 3}
    )
    assert odd == InvoiceDetail()
    s = summarize_details([d])
    assert s is not None
    assert PdfSummary.from_dict(s.as_dict()) == s
    assert PdfSummary.from_dict(None) is None
    assert PdfSummary.from_dict({"max_reached_on": "bad", "reached_kva_by_class": 1}) == (
        PdfSummary()
    )


def test_summary_over_several_invoices() -> None:
    winter = InvoiceDetail(
        invoice_date=date(2026, 2, 10),
        period_end=date(2026, 1, 24),
        reached_kva={"hph": 47.0, "hch": 30.0},
        overrun_hours={"hph": 2.0},
    )
    summer = InvoiceDetail(
        invoice_date=date(2026, 8, 10),
        period_end=date(2026, 7, 24),
        reached_kva={"hpe": 23.0},
        overrun_hours={"hpe": 0.0},
    )
    s = summarize_details([summer, winter])
    assert s is not None
    assert s.last == summer
    assert (s.max_reached_kva, s.max_reached_on) == (47.0, date(2026, 1, 24))
    assert s.reached_kva_by_class == {"hph": 47.0, "hch": 30.0, "hpe": 23.0}
    assert s.overrun_hours == 2.0
    assert s.invoices == 2
    gas = summarize_details([InvoiceDetail(invoice_date=date(2026, 8, 10))])
    assert gas is not None
    assert (gas.max_reached_kva, gas.overrun_hours) == (None, None)
    assert summarize_details([]) is None


def _pdf(rows: list[str]) -> bytes:
    """A one-page PDF with these rows in a monospaced font."""
    text = "".join(
        f"BT /F1 9 Tf 20 {800 - 12 * n} Td ({r.replace('(', '[').replace(')', ']')}) Tj ET\n"
        for n, r in enumerate(rows)
    )
    stream = text.encode("cp1252")  # WinAnsiEncoding: € is 0x80
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_reading_a_pdf() -> None:
    data = _pdf(
        [
            "Date de facture : 10/10/25",
            "Fourniture de gaz naturel                          80,00 €",
            "Consommation        02/09/25  01/10/25   2 000 kWh   0,04000 €/kWh   80,00 €",
            "Consommation Annuelle de Référence : 123 456 kWh",
        ]
    )
    assert "Consommation" in pdf_text(data)
    d = read_invoice(data)
    assert d.references["car_kwh"] == 123456.0
    assert d.kwh == 2000.0
    assert d.marginal_price == pytest.approx(0.04)
