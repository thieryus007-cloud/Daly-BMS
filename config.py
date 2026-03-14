"""
config.py — Configuration BMS centralisée (I7)
Source unique pour les noms, capacités et IDs des BMS.
Tous les modules importent depuis ce fichier au lieu de définir chacun
leur propre _load_bms_names() avec des variables d'env différentes.

Variables d'environnement :
    DALY_ADDRESSES  — ex. "0x01,0x02"  (défaut : "0x01,0x02")
    BMS{N}_NAME     — ex. BMS1_NAME="Pack 320Ah"
    BMS{N}_CAPACITY_AH — ex. BMS1_CAPACITY_AH="320"

Installation Santuario — Badalucco
"""

import os


def _parse_bms_ids() -> list[int]:
    raw = os.getenv("DALY_ADDRESSES", "0x01,0x02")
    return sorted({int(x.strip(), 0) for x in raw.split(",") if x.strip()})


BMS_IDS: list[int] = _parse_bms_ids()

BMS_NAMES: dict[int, str] = {
    bid: os.getenv(f"BMS{bid}_NAME", f"BMS {bid}")
    for bid in BMS_IDS
}

BMS_CAPACITY_AH: dict[int, float] = {
    bid: float(os.getenv(f"BMS{bid}_CAPACITY_AH", "320"))
    for bid in BMS_IDS
}
