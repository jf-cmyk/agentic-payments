import sys

filepath = 'src/config.py'
with open(filepath, 'r') as f:
    content = f.read()

replacement = """
try:
    settings = Settings()
except Exception as e:
    import builtins
    import os
    builtins.print("====== CRASH ENV VARS DEBUG ======")
    builtins.print(list(os.environ.keys()))
    builtins.print("==================================")
    raise e
"""
content = content.replace("settings = Settings()\n", replacement)

with open(filepath, 'w') as f:
    f.write(content)
