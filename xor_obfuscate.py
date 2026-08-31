#!/usr/bin/env python3
"""
XOR String Obfuscator for Python Source Code
Used as a pre-processing step before Nuitka native C compilation.
"""
import os
import re

XOR_KEY = 0x5A

def generate_xor_helper_code():
    return f"""
# XOR Runtime Helper
def _x(b):
    return bytes(x ^ {XOR_KEY} for x in b).decode('utf-8')
"""

def obfuscate_string_constants_in_file(filepath: str):
    if not os.path.exists(filepath):
        return

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # If already obfuscated, skip
    if "_x(" in content and "def _x(" in content:
        return

    helper = f"\n# XOR Runtime Helper\ndef _x(b):\n    return bytes(x ^ {XOR_KEY} for x in b).decode('utf-8')\n\n"

    import_match = list(re.finditer(r'^(import\s+.*|from\s+.*import\s+.*)$', content, re.MULTILINE))
    if import_match:
        last_import = import_match[-1]
        insert_pos = last_import.end()
        content = content[:insert_pos] + helper + content[insert_pos:]
    else:
        content = helper + content

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"[XOR] Injected XOR helper into {filepath}")

def main():
    target_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"[XOR Obfuscator] Processing Python files in {target_dir}...")
    
    python_files = []
    ignore_dirs = {'venv', '__pycache__', '.git', 'note-server-nuitka-build', 'backend'}
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for file in files:
            if file.endswith('.py') and file != 'xor_obfuscate.py':
                python_files.append(os.path.join(root, file))

    for py_file in python_files:
        obfuscate_string_constants_in_file(py_file)

    print(f"[XOR Obfuscator] Finished processing {len(python_files)} Python files.")

if __name__ == '__main__':
    main()
