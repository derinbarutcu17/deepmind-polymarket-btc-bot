"""Unit tests for AsyncPMClient with mocked sync_client."""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


@pytest.fixture
def mock_pm_client():
    """Create an AsyncPMClient with ALL externals mocked."""
    with patch("polymarket_client.ClobClient"), \
         patch("polymarket_client.aiohttp.ClientSession") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.close = AsyncMock()
        mock_session_cls.return_value = mock_session

        from polymarket_client import AsyncPMClient
        client = AsyncPMClient()
        client.sync_client = MagicMock()
        yield client


class TestPlaceLimitOrder:
    @pytest.mark.asyncio
    async def test_successful_order_returns_id(self, mock_pm_client):
        """Successful order should return the orderID."""
        mock_pm_client.sync_client.create_order.return_value = {"signed": True}
        mock_pm_client.sync_client.post_order.return_value = {
            "success": True,
            "orderID": "abc123def456",
        }

        result = await mock_pm_client.place_limit_order("tok1", "BUY", 0.50, 10.0)
        assert result == "abc123def456"

    @pytest.mark.asyncio
    async def test_rejected_order_returns_none(self, mock_pm_client):
        """Rejected order should return None."""
        mock_pm_client.sync_client.create_order.return_value = {"signed": True}
        mock_pm_client.sync_client.post_order.return_value = {
            "success": False,
            "errorMsg": "Crossed spread",
        }

        result = await mock_pm_client.place_limit_order("tok1", "BUY", 0.50, 10.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, mock_pm_client):
        """API exception should return None, not crash."""
        mock_pm_client.sync_client.create_order.side_effect = Exception("Network error")

        result = await mock_pm_client.place_limit_order("tok1", "BUY", 0.50, 10.0)
        assert result is None


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_successful_cancel(self, mock_pm_client):
        """Successful cancel should return True."""
        mock_pm_client.sync_client.cancel.return_value = {"canceled": True}
        result = await mock_pm_client.cancel_order("order123")
        assert result is True

    @pytest.mark.asyncio
    async def test_failed_cancel(self, mock_pm_client):
        """Failed cancel should return False."""
        mock_pm_client.sync_client.cancel.return_value = None
        result = await mock_pm_client.cancel_order("order123")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_exception(self, mock_pm_client):
        """Exception during cancel should return False."""
        mock_pm_client.sync_client.cancel.side_effect = Exception("Timeout")
        result = await mock_pm_client.cancel_order("order123")
        assert result is False
