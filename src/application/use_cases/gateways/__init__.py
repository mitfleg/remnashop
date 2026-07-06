from typing import Final

from src.application.common.interactor import Interactor

from .commands.configuration import (
    MovePaymentGatewayUp,
    ResetPaymentGatewaySettingsField,
    TogglePaymentGatewayActive,
    UpdatePaymentGatewaySettings,
)
from .commands.payment import (
    CreateDefaultPaymentGateway,
    CreatePayment,
    CreateTestPayment,
    ProcessPayment,
    RetryFailedTransaction,
)
from .queries.providers import GetPaymentGatewayInstance

GATEWAYS_USE_CASES: Final[tuple[type[Interactor], ...]] = (
    GetPaymentGatewayInstance,
    MovePaymentGatewayUp,
    TogglePaymentGatewayActive,
    UpdatePaymentGatewaySettings,
    ResetPaymentGatewaySettingsField,
    CreateDefaultPaymentGateway,
    CreatePayment,
    CreateTestPayment,
    ProcessPayment,
    RetryFailedTransaction,
)
