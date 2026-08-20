-- Ручной сценарий для применения только после отдельного согласования и бэкапа.
-- В миграции приложения не входит и автоматически не запускается.

BEGIN;

UPDATE plans SET description = E'Бесплатный доступ на 3 дня для знакомства с сервисом.\n10 ГБ трафика и до 4 устройств в одной подписке.\nВсе основные локации и стабильное подключение.' WHERE name = 'Start';
UPDATE plans SET description = E'Личный тариф для одного устройства.\n100 ГБ трафика — достаточно для сайтов, мессенджеров, видео и повседневных задач.\nВсе основные локации и стабильное подключение.' WHERE name = 'Light';
UPDATE plans SET description = E'Безлимитный интернет для повседневного использования.\nДо 4 устройств в одной подписке — например, телефон, компьютер, планшет и телевизор.\nВсе доступные локации, высокая скорость и автоматическое обновление серверов.' WHERE name = 'Premium';
UPDATE plans SET description = E'Расширенный Premium для семьи или большого количества техники.\nБезлимитный интернет и до 8 устройств в одной подписке.\nВсе доступные локации, высокая скорость и автоматическое обновление серверов.' WHERE name = 'Premium 8';
UPDATE plans SET description = E'Специальный тариф для своих — для тех, кто в ритме бачаты.\nБезлимитный интернет и до 4 устройств в одной подписке.\nВсе стандартные локации и стабильное подключение.\nТариф не включает специальные маршруты BlackRoute.' WHERE name = 'Bachata';
UPDATE plans SET description = E'Персональный тариф без ограничений по трафику и количеству устройств.\nДоступен только пользователям из закрытого списка.' WHERE name = 'Unlimited';

UPDATE plan_prices AS pp
SET price = catalog_price.price
FROM plan_durations AS pd
JOIN plans AS p ON p.id = pd.plan_id
JOIN (
    VALUES
        (7, 'RUB'::currency, 350.00::numeric),
        (7, 'XTR'::currency, 175.00::numeric),
        (7, 'USD'::currency, 4.60::numeric),
        (30, 'RUB'::currency, 1100.00::numeric),
        (30, 'XTR'::currency, 550.00::numeric),
        (30, 'USD'::currency, 15.70::numeric),
        (90, 'RUB'::currency, 3000.00::numeric),
        (90, 'XTR'::currency, 1500.00::numeric),
        (90, 'USD'::currency, 40.00::numeric)
) AS catalog_price(days, currency, price)
    ON catalog_price.days = pd.days
WHERE pp.plan_duration_id = pd.id
  AND pp.currency = catalog_price.currency
  AND p.name = 'Premium 8';

COMMIT;
