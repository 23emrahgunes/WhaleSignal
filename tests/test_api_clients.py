import pytest
from src.api_clients import GammaClient, DataClient, CLOBClient

def test_gamma_client_init():
    client = GammaClient()
    assert client.base_url == "https://gamma-api.polymarket.com"

def test_data_client_init():
    client = DataClient()
    assert client.base_url == "https://data-api.polymarket.com"

def test_clob_client_init():
    client = CLOBClient()
    assert client.base_url == "https://clob.polymarket.com"
