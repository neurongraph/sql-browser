"""Create a sample SQLite database for testing SQL Browser."""

import sqlite3
from datetime import datetime, timedelta
import random


def create_sample_database(db_path: str = "sample.db"):
    """Create a sample e-commerce database.
    
    Args:
        db_path: Path where the database will be created
    """
    # Connect to database (creates if doesn't exist)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            city TEXT,
            state TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)
    
    # Sample data
    customers = [
        ("Alice Johnson", "alice@example.com", "San Francisco", "CA"),
        ("Bob Smith", "bob@example.com", "New York", "NY"),
        ("Carol White", "carol@example.com", "Los Angeles", "CA"),
        ("David Brown", "david@example.com", "Chicago", "IL"),
        ("Eve Davis", "eve@example.com", "Houston", "TX"),
        ("Frank Miller", "frank@example.com", "Phoenix", "AZ"),
        ("Grace Wilson", "grace@example.com", "Philadelphia", "PA"),
        ("Henry Moore", "henry@example.com", "San Antonio", "TX"),
        ("Ivy Taylor", "ivy@example.com", "San Diego", "CA"),
        ("Jack Anderson", "jack@example.com", "Dallas", "TX"),
    ]
    
    products = [
        ("Laptop", "Electronics", 999.99, 50),
        ("Smartphone", "Electronics", 699.99, 100),
        ("Headphones", "Electronics", 149.99, 200),
        ("Desk Chair", "Furniture", 299.99, 30),
        ("Standing Desk", "Furniture", 599.99, 20),
        ("Monitor", "Electronics", 399.99, 75),
        ("Keyboard", "Electronics", 89.99, 150),
        ("Mouse", "Electronics", 49.99, 200),
        ("Webcam", "Electronics", 79.99, 100),
        ("Desk Lamp", "Furniture", 39.99, 80),
        ("Office Chair", "Furniture", 249.99, 40),
        ("Bookshelf", "Furniture", 179.99, 25),
        ("USB Cable", "Accessories", 12.99, 500),
        ("Phone Case", "Accessories", 19.99, 300),
        ("Screen Protector", "Accessories", 9.99, 400),
    ]
    
    # Insert customers
    cursor.executemany(
        "INSERT INTO customers (name, email, city, state) VALUES (?, ?, ?, ?)",
        customers
    )
    
    # Insert products
    cursor.executemany(
        "INSERT INTO products (name, category, price, stock) VALUES (?, ?, ?, ?)",
        products
    )
    
    # Generate random orders
    statuses = ['pending', 'processing', 'shipped', 'delivered', 'cancelled']
    
    for customer_id in range(1, 11):
        # Each customer makes 1-5 orders
        num_orders = random.randint(1, 5)
        
        for _ in range(num_orders):
            # Random date in the last 90 days
            days_ago = random.randint(0, 90)
            order_date = datetime.now() - timedelta(days=days_ago)
            
            # Random status (more likely to be delivered for older orders)
            if days_ago > 30:
                status = random.choice(['delivered', 'delivered', 'delivered', 'cancelled'])
            elif days_ago > 7:
                status = random.choice(['shipped', 'delivered', 'delivered'])
            else:
                status = random.choice(['pending', 'processing', 'shipped'])
            
            # Create order
            cursor.execute(
                "INSERT INTO orders (customer_id, order_date, total_amount, status) VALUES (?, ?, ?, ?)",
                (customer_id, order_date, 0, status)  # Will update total_amount later
            )
            order_id = cursor.lastrowid
            
            # Add 1-4 items to the order
            num_items = random.randint(1, 4)
            total_amount = 0
            
            for _ in range(num_items):
                product_id = random.randint(1, 15)
                quantity = random.randint(1, 3)
                
                # Get product price
                cursor.execute("SELECT price FROM products WHERE id = ?", (product_id,))
                price = cursor.fetchone()[0]
                
                item_total = price * quantity
                total_amount += item_total
                
                cursor.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (?, ?, ?, ?)",
                    (order_id, product_id, quantity, price)
                )
            
            # Update order total
            cursor.execute(
                "UPDATE orders SET total_amount = ? WHERE id = ?",
                (total_amount, order_id)
            )
    
    conn.commit()
    conn.close()
    
    print(f"✅ Sample database created: {db_path}")
    print("\nDatabase contains:")
    print("  • 10 customers")
    print("  • 15 products")
    print("  • ~30 orders with multiple items")
    print("\nYou can now run: uv run sql-browser sample.db")


if __name__ == "__main__":
    create_sample_database()

# Made with Bob
