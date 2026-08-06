import csv
import sqlite3

def build_database():
    # Connect to the SQLite database (it will create it if it doesn't exist)
    conn = sqlite3.connect('verified_shlokas.db')
    cursor = conn.cursor()

    # Drop the table if it exists so we can rebuild cleanly
    cursor.execute('DROP TABLE IF EXISTS verified_data')

    # Create the table with columns matching your CSV
    cursor.execute('''
        CREATE TABLE verified_data (
            id TEXT,
            chapter INTEGER,
            verse INTEGER,
            shloka TEXT,
            transliteration TEXT,
            hin_meaning TEXT,
            eng_meaning TEXT,
            word_meaning TEXT
        )
    ''')

    # Read the CSV and insert the data
    with open('gita.csv', 'r', encoding='utf-8') as file:
        # Use csv.DictReader to automatically handle the header row and commas inside quotes
        reader = csv.DictReader(file)
        for row in reader:
            cursor.execute('''
                INSERT INTO verified_data 
                (id, chapter, verse, shloka, transliteration, hin_meaning, eng_meaning, word_meaning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                row['ID'], row['Chapter'], row['Verse'], row['Shloka'], 
                row['Transliteration'], row['HinMeaning'], row['EngMeaning'], row['WordMeaning']
            ))

    conn.commit()
    conn.close()
    print("✅ Success! verified_shlokas.db has been built from gita.csv")

if __name__ == '__main__':
    build_database()