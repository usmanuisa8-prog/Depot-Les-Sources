import sqlite3

connexion = sqlite3.connect("depot.db")
curseur = connexion.cursor()

curseur.execute("""
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT,
    prix_detail INTEGER,
    prix_gros INTEGER,
    stock INTEGER
)
""")

articles = [
    ("Eau minérale 0.5L", 200, 150, 500),
    ("Eau minérale 1.5L", 500, 400, 300),
    ("Jus d'orange 1L", 1200, 1000, 150),
    ("Jus ananas 33cl", 500, 400, 200),
    ("Canette Coca 33cl", 600, 500, 250),
    ("Canette Fanta 33cl", 600, 500, 250),
]
curseur.executemany(
    "INSERT INTO articles (nom, prix_detail, prix_gros, stock) VALUES (?, ?, ?, ?)",
    articles
)

connexion.commit()
connexion.close()
print("Base de données créée avec 6 articles")