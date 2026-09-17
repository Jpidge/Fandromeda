import pyperclip

raw_text = pyperclip.paste()

print(f"Total characters copied: {len(raw_text)}")

# Write to a clean text file so we can view the exact line structure
with open("raw_dump.txt", "w", encoding="utf-8") as f:
    f.write(raw_text)

print("Saved raw copy to 'raw_dump.txt'.")
