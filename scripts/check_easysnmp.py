import easysnmp
print("easysnmp version:", easysnmp.__version__)
s = easysnmp.Session(hostname="127.0.0.1", community="x", version=2)
methods = [m for m in dir(s) if not m.startswith("_")]
print("Methods:", methods)
has_bulkwalk = hasattr(s, "bulkwalk")
has_getbulk = hasattr(s, "get_bulk") or hasattr(s, "getbulk")
print(f"has bulkwalk: {has_bulkwalk}")
print(f"has get_bulk: {has_getbulk}")

# Check if walk uses getBulk under the hood for v2c
import inspect
walk_src = inspect.getsource(s.walk)
uses_bulk = "bulk" in walk_src.lower()
print(f"walk source mentions bulk: {uses_bulk}")
print("walk source (first 500 chars):", walk_src[:500])
