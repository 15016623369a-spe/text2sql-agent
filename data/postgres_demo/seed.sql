\getenv reader_password RAG_POSTGRES_PASSWORD

BEGIN;
SELECT pg_advisory_xact_lock(741381);

CREATE SCHEMA IF NOT EXISTS retail AUTHORIZATION postgres;

CREATE TABLE IF NOT EXISTS retail.regions (
    region_id integer PRIMARY KEY,
    region_name text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS retail.customers (
    customer_id integer PRIMARY KEY,
    customer_name text NOT NULL,
    region_id integer NOT NULL REFERENCES retail.regions
);
CREATE TABLE IF NOT EXISTS retail.categories (
    category_id integer PRIMARY KEY,
    category_name text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS retail.products (
    product_id integer PRIMARY KEY,
    product_name text UNIQUE NOT NULL,
    category_id integer NOT NULL REFERENCES retail.categories,
    list_price numeric(12, 2) NOT NULL CHECK (list_price >= 0)
);
CREATE TABLE IF NOT EXISTS retail.warehouses (
    warehouse_id integer PRIMARY KEY,
    warehouse_name text UNIQUE NOT NULL,
    region_id integer NOT NULL REFERENCES retail.regions
);
CREATE TABLE IF NOT EXISTS retail.inventory (
    warehouse_id integer NOT NULL REFERENCES retail.warehouses,
    product_id integer NOT NULL REFERENCES retail.products,
    quantity_on_hand integer NOT NULL CHECK (quantity_on_hand >= 0),
    PRIMARY KEY (warehouse_id, product_id)
);
CREATE TABLE IF NOT EXISTS retail.orders (
    order_id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES retail.customers,
    warehouse_id integer NOT NULL REFERENCES retail.warehouses,
    order_date date NOT NULL,
    status text NOT NULL CHECK (status IN ('completed', 'processing', 'cancelled'))
);
CREATE TABLE IF NOT EXISTS retail.order_items (
    order_item_id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES retail.orders,
    product_id integer NOT NULL REFERENCES retail.products,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(12, 2) NOT NULL CHECK (unit_price >= 0)
);
CREATE TABLE IF NOT EXISTS retail.payments (
    payment_id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES retail.orders,
    method text NOT NULL CHECK (method IN ('card', 'bank')),
    status text NOT NULL CHECK (status IN ('captured', 'pending', 'voided')),
    amount numeric(12, 2) NOT NULL CHECK (amount > 0)
);
CREATE TABLE IF NOT EXISTS retail.refunds (
    refund_id integer PRIMARY KEY,
    payment_id integer NOT NULL REFERENCES retail.payments,
    refund_date date NOT NULL,
    reason text NOT NULL,
    amount numeric(12, 2) NOT NULL CHECK (amount > 0)
);

COMMENT ON TABLE retail.regions IS 'Sales territories used by customer and warehouse records.';
COMMENT ON TABLE retail.customers IS 'One row per retail customer account.';
COMMENT ON TABLE retail.categories IS 'Product categories used for merchandise reports.';
COMMENT ON TABLE retail.products IS 'Product catalog including current list price in CNY.';
COMMENT ON TABLE retail.warehouses IS 'Fulfillment hubs that ship orders.';
COMMENT ON TABLE retail.inventory IS 'Current available stock by warehouse and product, including zero stock.';
COMMENT ON TABLE retail.orders IS 'Sales order headers with placement date, customer and shipping warehouse.';
COMMENT ON TABLE retail.order_items IS 'One purchased product line per row. Gross line sales are quantity times unit_price.';
COMMENT ON TABLE retail.payments IS 'Payment transactions. An order may have multiple captured payments.';
COMMENT ON TABLE retail.refunds IS 'Refund transactions linked to individual payments. A payment may have multiple refunds.';
COMMENT ON COLUMN retail.orders.order_date IS 'Calendar date the customer placed the order.';
COMMENT ON COLUMN retail.orders.status IS 'Order workflow status: completed, processing or cancelled.';
COMMENT ON COLUMN retail.order_items.unit_price IS 'Price in CNY per unit at purchase time.';
COMMENT ON COLUMN retail.payments.status IS 'Only captured payments represent received money.';
COMMENT ON COLUMN retail.payments.amount IS 'Positive payment amount in CNY.';
COMMENT ON COLUMN retail.refunds.amount IS 'Positive refunded amount in CNY. Subtract it from captured payments for net revenue.';
COMMENT ON COLUMN retail.inventory.quantity_on_hand IS 'Available units in the selected warehouse now.';

INSERT INTO retail.regions VALUES
    (1, 'North'), (2, 'South'), (3, 'West')
ON CONFLICT DO NOTHING;
INSERT INTO retail.customers VALUES
    (1, 'Aster Retail', 1), (2, 'Beacon Market', 1), (3, 'Cedar Goods', 2),
    (4, 'Dune Outlet', 2), (5, 'Elm Supply', 3), (6, 'Fjord Store', 3)
ON CONFLICT DO NOTHING;
INSERT INTO retail.categories VALUES
    (1, 'Hardware'), (2, 'Accessories'), (3, 'Service')
ON CONFLICT DO NOTHING;
INSERT INTO retail.products VALUES
    (1, 'Apex Laptop', 1, 950), (2, 'Nova Monitor', 1, 220),
    (3, 'Orbit Keyboard', 2, 65), (4, 'Pico Mouse', 2, 30),
    (5, 'USB-C Hub', 2, 45), (6, 'Care Plan', 3, 90),
    (7, 'Metro Tablet', 1, 420), (8, 'Carry Case', 2, 35)
ON CONFLICT DO NOTHING;
INSERT INTO retail.warehouses VALUES
    (1, 'North Hub', 1), (2, 'South Hub', 2), (3, 'West Hub', 3)
ON CONFLICT DO NOTHING;
INSERT INTO retail.inventory VALUES
    (1, 1, 7), (1, 2, 25), (1, 3, 8), (1, 4, 30), (1, 5, 0), (1, 7, 6), (1, 8, 15),
    (2, 1, 6), (2, 2, 4), (2, 3, 25), (2, 4, 5), (2, 5, 15), (2, 7, 12), (2, 8, 20),
    (3, 1, 2), (3, 2, 10), (3, 3, 7), (3, 4, 20), (3, 5, 5), (3, 7, 7), (3, 8, 4)
ON CONFLICT DO NOTHING;
INSERT INTO retail.orders VALUES
    (1001, 1, 1, '2026-09-04', 'completed'), (1002, 3, 2, '2026-09-09', 'completed'),
    (1003, 5, 3, '2026-09-11', 'cancelled'), (1004, 2, 1, '2026-09-21', 'completed'),
    (1005, 4, 2, '2026-10-02', 'completed'), (1006, 6, 3, '2026-10-03', 'completed'),
    (1007, 1, 1, '2026-10-08', 'completed'), (1008, 3, 2, '2026-10-15', 'processing'),
    (1009, 5, 3, '2026-10-18', 'completed'), (1010, 2, 1, '2026-10-23', 'cancelled'),
    (1011, 4, 2, '2026-11-02', 'completed'), (1012, 6, 3, '2026-11-06', 'completed')
ON CONFLICT DO NOTHING;
INSERT INTO retail.order_items VALUES
    (1, 1001, 1, 1, 950), (2, 1001, 2, 2, 220),
    (3, 1002, 2, 1, 220), (4, 1002, 3, 2, 65),
    (5, 1003, 4, 3, 30), (6, 1004, 7, 2, 420), (7, 1004, 5, 2, 45),
    (8, 1005, 1, 2, 950), (9, 1005, 8, 2, 35),
    (10, 1006, 2, 3, 220), (11, 1006, 4, 3, 30),
    (12, 1007, 1, 1, 950), (13, 1007, 6, 1, 90), (14, 1008, 3, 3, 65),
    (15, 1009, 7, 1, 420), (16, 1009, 8, 1, 35), (17, 1010, 5, 4, 45),
    (18, 1011, 2, 2, 220), (19, 1011, 5, 2, 45),
    (20, 1012, 3, 4, 65), (21, 1012, 4, 2, 30)
ON CONFLICT DO NOTHING;
INSERT INTO retail.payments VALUES
    (1, 1001, 'card', 'captured', 1390), (2, 1002, 'bank', 'captured', 350),
    (3, 1003, 'card', 'voided', 90), (4, 1004, 'card', 'captured', 930),
    (5, 1005, 'bank', 'captured', 1000), (6, 1005, 'card', 'captured', 970),
    (7, 1006, 'card', 'captured', 750), (8, 1007, 'card', 'captured', 1040),
    (9, 1008, 'bank', 'pending', 195), (10, 1009, 'card', 'captured', 455),
    (11, 1010, 'card', 'voided', 180), (12, 1011, 'bank', 'captured', 530),
    (13, 1012, 'card', 'captured', 320)
ON CONFLICT DO NOTHING;
INSERT INTO retail.refunds VALUES
    (1, 2, '2026-09-12', 'accessory_return', 65), (2, 6, '2026-10-04', 'case_return', 35),
    (3, 7, '2026-10-07', 'damaged_monitor', 220), (4, 7, '2026-10-08', 'damaged_mouse', 30),
    (5, 10, '2026-10-20', 'case_return', 35), (6, 13, '2026-11-10', 'damaged_mouse', 30)
ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rag_reader') THEN
        CREATE ROLE rag_reader;
    END IF;
END $$;
ALTER ROLE rag_reader WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'reader_password';
ALTER ROLE rag_reader IN DATABASE rag_demo SET default_transaction_read_only = on;
ALTER ROLE rag_reader IN DATABASE rag_demo SET search_path TO pg_catalog, retail;

REVOKE ALL ON DATABASE rag_demo FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres, template1 FROM PUBLIC;
GRANT CONNECT ON DATABASE rag_demo TO rag_reader;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA retail TO rag_reader;
GRANT SELECT ON retail.regions, retail.customers, retail.categories, retail.products, retail.warehouses,
    retail.inventory, retail.orders, retail.order_items, retail.payments, retail.refunds TO rag_reader;

COMMIT;
