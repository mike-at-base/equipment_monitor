"""
One-shot async browse of ST40001's EM node to find sequence name/description paths.
Run from repo root: python browse_seq.py
"""
import asyncio
import sys
from asyncua import Client, ua

ENDPOINT = "opc.tcp://10.200.2.134:4840"
NS = 3
EM_PATH = '"ST40001_Station_DB".mainEquipmentModule'


async def try_read(client, path):
    try:
        node = client.get_node(ua.NodeId(path, NS))
        val = await node.read_value()
        return val
    except Exception as e:
        return f"ERR: {e}"


async def browse_children(client, node_id_str, indent=0):
    """Recursively browse children up to depth 2."""
    try:
        node = client.get_node(ua.NodeId(node_id_str, NS))
        children = await node.get_children()
        for child in children:
            try:
                name = (await child.read_browse_name()).Name
                nid = child.nodeid.Identifier
                try:
                    val = await child.read_value()
                    print(f"{'  '*indent}{name!r:40s}  = {val!r}")
                except Exception:
                    print(f"{'  '*indent}{name!r:40s}  [no value — node]")
                    if indent < 1:
                        await browse_children_by_node(client, child, indent+1)
            except Exception as e:
                print(f"{'  '*indent}  [browse error: {e}]")
    except Exception as e:
        print(f"{'  '*indent}browse_children({node_id_str!r}) failed: {e}")


async def browse_children_by_node(client, node, indent=0):
    try:
        children = await node.get_children()
        for child in children:
            try:
                name = (await child.read_browse_name()).Name
                try:
                    val = await child.read_value()
                    print(f"{'  '*indent}{name!r:40s}  = {val!r}")
                except Exception:
                    print(f"{'  '*indent}{name!r:40s}  [node]")
            except Exception:
                pass
    except Exception:
        pass


async def main():
    print(f"Connecting to {ENDPOINT} ...")
    async with Client(url=ENDPOINT) as client:
        print(f"Connected.\n")

        # 1. Try to read known paths and look for sequence name/description
        probe_paths = [
            # Possible sequence name fields at seqControl level
            f'{EM_PATH}.seqControl[1].name',
            f'{EM_PATH}.seqControl[1].seqName',
            f'{EM_PATH}.seqControl[1].description',
            f'{EM_PATH}.seqControl[1].seqDescription',
            f'{EM_PATH}.seqControl[1].sequenceName',
            # At stepControl level (maybe seq name is there)
            f'{EM_PATH}.stepControl[1].seqName',
            f'{EM_PATH}.stepControl[1].sequenceName',
            f'{EM_PATH}.stepControl[1].name',
            # Configuration sub-structure
            f'{EM_PATH}.configuration[1].name',
            f'{EM_PATH}.configuration[1].description',
            f'{EM_PATH}.sequenceControl[1].name',
            f'{EM_PATH}.sequenceControl[1].description',
            # Direct EM-level
            f'{EM_PATH}.status.activeSequence',
            f'{EM_PATH}.status.activeSequenceName',
        ]

        print("=== Probing candidate paths ===")
        for path in probe_paths:
            val = await try_read(client, path)
            print(f"  {path[len(EM_PATH)+1:]:50s}  =>  {val!r}")

        # 2. Browse stepControl[1] children to see all available fields
        print(f"\n=== Browse stepControl[1] children ===")
        await browse_children(client, f'{EM_PATH}.stepControl[1]')

        # 3. Browse top-level EM children to see if seqControl / configuration exists
        print(f"\n=== Browse EM top-level children ===")
        await browse_children(client, EM_PATH)


if __name__ == "__main__":
    asyncio.run(main())
