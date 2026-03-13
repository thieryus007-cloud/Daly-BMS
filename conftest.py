"""
conftest.py — Configuration pytest pour Daly BMS Interface
Enregistrement des markers, logique de skip pour tests hardware/intégration.
"""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-hardware",
        action="store_true",
        default=False,
        help="Exécute les tests nécessitant le matériel (UART, BMS physique)",
    )
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Exécute les tests d'intégration (Mosquitto local requis)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "hardware: test nécessitant le BMS physique et le port UART (--run-hardware)",
    )
    config.addinivalue_line(
        "markers",
        "integration: test nécessitant Mosquitto local (--run-integration)",
    )


def pytest_collection_modifyitems(config, items):
    skip_hardware = pytest.mark.skip(
        reason="Tests matériel ignorés — utiliser --run-hardware pour les activer"
    )
    skip_integration = pytest.mark.skip(
        reason="Tests d'intégration ignorés — utiliser --run-integration pour les activer"
    )

    for item in items:
        if "hardware" in item.keywords and not config.getoption("--run-hardware"):
            item.add_marker(skip_hardware)
        if "integration" in item.keywords and not config.getoption("--run-integration"):
            item.add_marker(skip_integration)
