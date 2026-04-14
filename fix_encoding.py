import re

def fix_encoding(text):
    # Decode double-encoded UTF-8
    try:
        # First decode from latin1 to bytes, then decode as utf-8
        return text.encode('latin1').decode('utf-8')
    except:
        return text

def fix_file(filepath):
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # Find all strings with Ã and fix them
    def replace_match(match):
        string_content = match.group(1)
        fixed = fix_encoding(string_content)
        return f'"{fixed}"'

    # Pattern for strings containing Ã
    pattern = r'"([^"]*Ã[^"]*)"'
    new_content = re.sub(pattern, replace_match, content)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        fix_file(sys.argv[1])