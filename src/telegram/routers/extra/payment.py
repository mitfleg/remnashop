from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.types import Message, PreCheckoutQuery
from dishka import FromDishka
from loguru import logger

from src.application.common.dao import TransactionDao
from src.application.dto import TelegramUserDto
from src.application.use_cases.gateways.commands.payment import ProcessPayment, ProcessPaymentDto
from src.core.enums import PaymentGatewayType, TransactionStatus

router = Router(name=__name__)


@router.pre_checkout_query()
async def on_pre_checkout(
    pre_checkout_query: PreCheckoutQuery,
    user: TelegramUserDto,
    transaction_dao: FromDishka[TransactionDao],
) -> None:
    logger.info(f"{user.log} Initiated a pre-checkout query")
    try:
        transaction = await transaction_dao.get_by_payment_id(
            UUID(pre_checkout_query.invoice_payload)
        )
    except ValueError:
        transaction = None
    valid = bool(
        transaction
        and transaction.status == TransactionStatus.PENDING
        and transaction.gateway_type == PaymentGatewayType.TELEGRAM_STARS
        and transaction.user_id == user.id
        and transaction.currency.value == pre_checkout_query.currency
        and transaction.pricing.final_amount == pre_checkout_query.total_amount
    )
    await pre_checkout_query.answer(
        ok=valid,
        error_message=None if valid else "Счёт устарел. Откройте покупку в боте заново.",
    )


@router.message(F.successful_payment)
async def on_successful_payment(
    message: Message,
    user: TelegramUserDto,
    bot: Bot,
    process_payment: FromDishka[ProcessPayment],
    transaction_dao: FromDishka[TransactionDao],
) -> None:
    payment = message.successful_payment

    if not payment:
        return

    try:
        transaction = await transaction_dao.get_by_payment_id(UUID(payment.invoice_payload))
    except ValueError:
        transaction = None
    if not transaction or transaction.user_id != user.id:
        logger.error("Stars payment does not match its purchaser")
        return
    if (
        transaction.currency.value != payment.currency
        or transaction.pricing.final_amount != payment.total_amount
    ):
        logger.error("Stars payment amount or currency mismatch")
        return
    new_status = TransactionStatus.COMPLETED
    if user.is_owner and transaction.gift_id is None:
        logger.info(f"{user.log} Refunding test payment '{payment.telegram_payment_charge_id}'")
        await bot.refund_star_payment(
            user_id=user.telegram_id,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
        )
        new_status = TransactionStatus.CANCELED
    await process_payment.system(
        ProcessPaymentDto(
            payment_id=UUID(payment.invoice_payload),
            new_transaction_status=new_status,
            gateway_type=PaymentGatewayType.TELEGRAM_STARS,
        )
    )
