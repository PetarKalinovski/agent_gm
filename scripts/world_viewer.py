#!/usr/bin/env python3
"""Streamlit app to visualize a generated game world.

Usage:
    streamlit run scripts/world_viewer.py

Or with custom database:
    streamlit run scripts/world_viewer.py -- --db data/my_world.db
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import streamlit as st

# Must be first Streamlit command
st.set_page_config(
    page_title="World Viewer",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded"
)

from src.models import init_db, get_session
from src.models import (
    Faction, FactionRelationship, Location, NPC, WorldBible,
    HistoricalEvent, WorldClock, Event, Connection
)


# Initialize DB
@st.cache_resource
def init_database(db_path: str = "data/sw.db"):
    init_db(db_path)
    return True


def get_world_bible():
    with get_session() as session:
        return session.query(WorldBible).first()


def get_factions():
    with get_session() as session:
        return session.query(Faction).all()


def get_faction_relationships():
    with get_session() as session:
        return session.query(FactionRelationship).all()


def get_locations():
    with get_session() as session:
        return session.query(Location).all()


def get_npcs():
    with get_session() as session:
        return session.query(NPC).all()


def get_historical_events():
    with get_session() as session:
        return session.query(HistoricalEvent).all()


def get_runtime_events():
    with get_session() as session:
        return session.query(Event).all()


def get_world_clock():
    with get_session() as session:
        return session.query(WorldClock).first()


def get_connections():
    with get_session() as session:
        return session.query(Connection).all()


def delete_entity(entity_type, entity_id):
    """Delete an entity from the database."""
    with get_session() as session:
        if entity_type == "location":
            entity = session.get(Location, entity_id)
        elif entity_type == "npc":
            entity = session.get(NPC, entity_id)
        elif entity_type == "faction":
            entity = session.get(Faction, entity_id)
        elif entity_type == "connection":
            entity = session.get(Connection, entity_id)
        else:
            return False

        if entity:
            session.delete(entity)
            session.commit()
            return True
    return False


# Initialize
init_database()

# Sidebar Navigation
st.sidebar.title("🌍 World Viewer")
page = st.sidebar.radio(
    "Navigate",
    ["Overview", "Factions", "Locations", "Connections", "NPCs", "History", "Events", "Raw Data"]
)

# Main content
if page == "Overview":
    st.title("🌍 World Overview")

    bible = get_world_bible()
    if not bible:
        st.error("No world found! Run `python scripts/generate_world.py` first.")
        st.stop()

    # World Bible Card
    col1, col2 = st.columns([2, 1])

    with col1:
        st.header(f"📖 {bible.name}")
        st.markdown(f"**Genre:** {bible.genre} ({', '.join(bible.sub_genres or [])})")
        st.markdown(f"**Tone:** {bible.tone}")
        st.markdown(f"**Themes:** {', '.join(bible.themes or [])}")

        with st.expander("Setting Description"):
            st.write(bible.setting_description)

        with st.expander("Current Situation"):
            st.write(bible.current_situation)

        with st.expander("World Rules"):
            for rule in (bible.rules or []):
                st.markdown(f"- {rule}")

    with col2:
        # Stats
        factions = get_factions()
        locations = get_locations()
        npcs = get_npcs()
        events = get_historical_events()

        st.metric("Factions", len(factions))
        st.metric("Locations", len(locations))
        st.metric("NPCs", len(npcs))
        st.metric("Historical Events", len(events))

        clock = get_world_clock()
        if clock:
            st.markdown("---")
            st.markdown(f"**Current Time:** Day {clock.day}, {clock.hour:02d}:00")

    # Quick faction overview
    st.header("⚔️ Faction Power Balance")
    if factions:
        import pandas as pd
        faction_data = [
            {"Name": f.name, "Power": f.power_level, "Ideology": (f.ideology[:50] + "..." if len(f.ideology) > 50 else f.ideology)}
            for f in factions
        ]
        df = pd.DataFrame(faction_data)
        st.bar_chart(df.set_index("Name")["Power"])

elif page == "Factions":
    st.title("⚔️ Factions")

    factions = get_factions()
    relationships = get_faction_relationships()

    if not factions:
        st.warning("No factions found.")
        st.stop()

    # Faction selector
    faction_names = {f.id: f.name for f in factions}
    selected_faction_id = st.selectbox(
        "Select Faction",
        options=[f.id for f in factions],
        format_func=lambda x: faction_names[x]
    )

    selected = next(f for f in factions if f.id == selected_faction_id)

    col1, col2 = st.columns([2, 1])

    with col1:
        st.header(selected.name)
        st.markdown(f"**Ideology:** {selected.ideology}")
        st.markdown(f"**Methods:** {', '.join(selected.methods or [])}")
        st.markdown(f"**Aesthetic:** {selected.aesthetic}")

        if selected.leadership:
            st.markdown(f"**Leader:** {selected.leadership.get('leader_name', 'Unknown')}")
            st.markdown(f"**Structure:** {selected.leadership.get('structure_type', 'Unknown')}")

        with st.expander("Goals"):
            st.markdown("**Short-term:**")
            for g in (selected.goals_short or []):
                st.markdown(f"- {g}")
            st.markdown("**Long-term:**")
            for g in (selected.goals_long or []):
                st.markdown(f"- {g}")

        with st.expander("🔒 Secrets", expanded=False):
            for s in (selected.secrets or []):
                st.markdown(f"- {s}")

        with st.expander("History Notes"):
            for h in (selected.history_notes or []):
                st.markdown(f"- {h}")

    with col2:
        st.subheader("Power & Resources")
        st.metric("Power Level", f"{selected.power_level}/100")

        if selected.resources:
            for key, value in selected.resources.items():
                st.progress(value / 100, text=f"{key.title()}: {value}")

        # Relationships
        st.subheader("Relationships")
        faction_rels = [r for r in relationships if r.faction_a_id == selected.id or r.faction_b_id == selected.id]
        for rel in faction_rels:
            other_id = rel.faction_b_id if rel.faction_a_id == selected.id else rel.faction_a_id
            other_name = faction_names.get(other_id, "Unknown")
            emoji = {"allied": "🤝", "neutral": "😐", "rival": "😠", "war": "⚔️", "vassal": "👑"}.get(rel.relationship_type, "❓")
            st.markdown(f"{emoji} **{other_name}**: {rel.relationship_type} (stability: {rel.stability})")

        # Edit form
        st.markdown("---")
        with st.form(key=f"edit_faction_{selected.id}"):
            st.markdown("**Edit Faction**")
            new_power = st.slider("Power Level", min_value=0, max_value=100, value=selected.power_level)

            if st.form_submit_button("Save Changes"):
                with get_session() as session:
                    faction_obj = session.get(Faction, selected.id)
                    if faction_obj:
                        faction_obj.power_level = new_power
                        session.commit()
                        st.success(f"{selected.name} updated!")
                        st.rerun()

        # Delete button
        if st.button(f"🗑️ Delete {selected.name}", key=f"delete_faction_{selected.id}"):
            if st.session_state.get(f"confirm_delete_faction_{selected.id}", False):
                if delete_entity("faction", selected.id):
                    st.success(f"Deleted {selected.name}")
                    st.rerun()
                else:
                    st.error("Failed to delete faction")
            else:
                st.session_state[f"confirm_delete_faction_{selected.id}"] = True
                st.warning("Click again to confirm deletion")

    # Relationship Matrix
    st.header("Faction Relationship Matrix")
    if len(factions) > 1:
        import pandas as pd
        matrix_data = {}
        for f in factions:
            matrix_data[f.name] = {}
            for f2 in factions:
                if f.id == f2.id:
                    matrix_data[f.name][f2.name] = "-"
                else:
                    rel = next((r for r in relationships if
                               (r.faction_a_id == f.id and r.faction_b_id == f2.id) or
                               (r.faction_a_id == f2.id and r.faction_b_id == f.id)), None)
                    if rel:
                        matrix_data[f.name][f2.name] = rel.relationship_type
                    else:
                        matrix_data[f.name][f2.name] = "neutral"
        df = pd.DataFrame(matrix_data)
        st.dataframe(df, use_container_width=True)

elif page == "Locations":
    st.title("🗺️ Locations")

    locations = get_locations()
    factions = get_factions()
    faction_names = {f.id: f.name for f in factions}

    if not locations:
        st.warning("No locations found.")
        st.stop()

    # Check for multiple root locations
    root_locations = [l for l in locations if l.parent_id is None]
    if len(root_locations) > 1:
        st.warning(f"⚠️ Found {len(root_locations)} root-level locations. Typically there should be only 1 (the galaxy/world).")
        st.markdown("**Root locations:**")
        for root in root_locations:
            st.markdown(f"- {root.name} ({root.type.value})")
        st.info("💡 To fix: Select a location below, then change its 'Parent Location' in the edit form.")
        st.divider()

    # Build hierarchy
    def build_tree(parent_id=None, depth=0):
        children = [l for l in locations if l.parent_id == parent_id]
        result = []
        for child in children:
            result.append((child, depth))
            result.extend(build_tree(child.id, depth + 1))
        return result


    # Get root locations and orphaned locations
    tree = build_tree()
    displayed_ids = {loc.id for loc, _ in tree}
    orphaned = [loc for loc in locations if loc.id not in displayed_ids]

    # Add orphaned locations at root level
    for orphan in orphaned:
        tree.append((orphan, 0))

    # Tree view
    st.header("Location Hierarchy")
    for loc, depth in tree:
        indent = "  " * depth
        icon = {"galaxy": "🌌", "world": "🌍", "sector": "🔷", "continent": "🏔️",
                "kingdom": "👑", "system": "☀️", "planet": "🪐", "settlement": "🏘️",
                "city": "🏙️", "town": "🏘️", "station": "🛸", "district": "📍",
                "building": "🏛️", "poi": "📌", "room": "🚪"}.get(loc.type.value, "📍")
        faction_info = f" [{faction_names.get(loc.controlling_faction_id, '')}]" if loc.controlling_faction_id else ""
        state_emoji = {"peaceful": "✅", "under_siege": "⚠️", "destroyed": "💀"}.get(loc.current_state, "")

        if st.button(f"{indent}{icon} {loc.name}{faction_info} {state_emoji}", key=loc.id):
            st.session_state.selected_location = loc.id

    # Location details
    st.header("Location Details")
    selected_id = st.session_state.get("selected_location", tree[0][0].id if tree else None)

    if selected_id:
        loc = next((l for l in locations if l.id == selected_id), None)
        if loc:
            col1, col2 = st.columns([2, 1])

            with col1:
                st.subheader(loc.name)
                st.markdown(f"**Type:** {loc.type.value}")
                st.markdown(f"**State:** {loc.current_state}")
                st.write(loc.description)

                if loc.atmosphere_tags:
                    st.markdown(f"**Atmosphere:** {', '.join(loc.atmosphere_tags)}")

                if loc.secrets:
                    with st.expander("🔒 Secrets"):
                        for s in loc.secrets:
                            st.markdown(f"- {s}")

            with col2:
                st.markdown(f"**Controlling Faction:** {faction_names.get(loc.controlling_faction_id, 'None')}")
                st.markdown(f"**Position:** ({loc.position_x:.0f}, {loc.position_y:.0f})")
                st.markdown(f"**Visited:** {'Yes' if loc.visited else 'No'}")
                st.markdown(f"**Discovered:** {'Yes' if loc.discovered else 'No'}")

                # NPCs at location
                npcs = get_npcs()
                npcs_here = [n for n in npcs if n.current_location_id == loc.id]
                if npcs_here:
                    st.markdown("**NPCs Here:**")
                    for npc in npcs_here:
                        st.markdown(f"- {npc.name} ({npc.profession})")

            # Connections for this location
            connections = get_connections()
            location_names_map = {l.id: l.name for l in locations}
            outgoing = [c for c in connections if c.from_location_id == loc.id]
            incoming = [c for c in connections if c.to_location_id == loc.id and not c.bidirectional]
            bidirectional_incoming = [c for c in connections if c.to_location_id == loc.id and c.bidirectional and c.from_location_id != loc.id]

            if outgoing or incoming or bidirectional_incoming:
                st.markdown("---")
                st.subheader("🔗 Connections")

                if outgoing:
                    for conn in outgoing:
                        dest_name = location_names_map.get(conn.to_location_id, "Unknown")
                        direction = "↔" if conn.bidirectional else "→"
                        discovered_icon = "✓" if conn.discovered else "?"
                        st.markdown(f"{direction} **{dest_name}** ({conn.travel_type}, {conn.travel_time_hours}h) {discovered_icon}")

                if incoming:
                    st.markdown("**Incoming (one-way):**")
                    for conn in incoming:
                        source_name = location_names_map.get(conn.from_location_id, "Unknown")
                        discovered_icon = "✓" if conn.discovered else "?"
                        st.markdown(f"← **{source_name}** ({conn.travel_type}, {conn.travel_time_hours}h) {discovered_icon}")

                if bidirectional_incoming:
                    for conn in bidirectional_incoming:
                        source_name = location_names_map.get(conn.from_location_id, "Unknown")
                        discovered_icon = "✓" if conn.discovered else "?"
                        st.markdown(f"↔ **{source_name}** ({conn.travel_type}, {conn.travel_time_hours}h) {discovered_icon}")
            else:
                st.markdown("---")
                st.info("No connections to other locations.")

            # Edit form
            st.markdown("---")
            with st.form(key=f"edit_loc_{loc.id}"):
                st.markdown("**Edit Location**")

                # Parent location selector (for fixing hierarchy)
                parent_options = [None] + [l.id for l in locations if l.id != loc.id]
                current_parent_index = 0
                if loc.parent_id in parent_options:
                    current_parent_index = parent_options.index(loc.parent_id)
                new_parent = st.selectbox(
                    "Parent Location",
                    options=parent_options,
                    format_func=lambda x: f"{next((l.name for l in locations if l.id == x), 'Unknown')}" if x else "(Root - No Parent)",
                    index=current_parent_index,
                    help="Set to (Root) to make this a top-level location"
                )

                new_state = st.selectbox("State", ["peaceful", "under_siege", "destroyed", "abandoned", "occupied"], index=["peaceful", "under_siege", "destroyed", "abandoned", "occupied"].index(loc.current_state) if loc.current_state in ["peaceful", "under_siege", "destroyed", "abandoned", "occupied"] else 0)
                new_discovered = st.checkbox("Discovered", value=loc.discovered)
                new_visited = st.checkbox("Visited", value=loc.visited)
                new_controlling_faction = st.selectbox("Controlling Faction", options=[None] + [f.id for f in factions], format_func=lambda x: faction_names.get(x, "None") if x else "None", index=([None] + [f.id for f in factions]).index(loc.controlling_faction_id) if loc.controlling_faction_id in [f.id for f in factions] else 0)

                # Position editing
                col_pos1, col_pos2 = st.columns(2)
                with col_pos1:
                    new_pos_x = st.number_input("Position X", min_value=0.0, max_value=100.0, value=float(loc.position_x), step=1.0)
                with col_pos2:
                    new_pos_y = st.number_input("Position Y", min_value=0.0, max_value=100.0, value=float(loc.position_y), step=1.0)

                if st.form_submit_button("Save Changes"):
                    with get_session() as session:
                        loc_obj = session.get(Location, loc.id)
                        if loc_obj:
                            loc_obj.parent_id = new_parent
                            loc_obj.current_state = new_state
                            loc_obj.discovered = new_discovered
                            loc_obj.visited = new_visited
                            loc_obj.controlling_faction_id = new_controlling_faction
                            loc_obj.position_x = new_pos_x
                            loc_obj.position_y = new_pos_y
                            session.commit()
                            st.success(f"{loc.name} updated!")
                            st.rerun()

            # Delete button
            if st.button(f"🗑️ Delete {loc.name}", key=f"delete_loc_{loc.id}"):
                if st.session_state.get(f"confirm_delete_loc_{loc.id}", False):
                    if delete_entity("location", loc.id):
                        st.success(f"Deleted {loc.name}")
                        st.rerun()
                    else:
                        st.error("Failed to delete location")
                else:
                    st.session_state[f"confirm_delete_loc_{loc.id}"] = True
                    st.warning("Click again to confirm deletion")

elif page == "Connections":
    st.title("🔗 Location Connections")

    connections = get_connections()
    locations = get_locations()
    location_names = {l.id: l.name for l in locations}

    st.markdown(f"**Total Connections:** {len(connections)}")

    # Add new connection form
    st.header("Add New Connection")
    with st.form(key="add_connection_form"):
        col1, col2 = st.columns(2)
        with col1:
            from_location = st.selectbox(
                "From Location",
                options=[l.id for l in locations],
                format_func=lambda x: location_names.get(x, "Unknown"),
                key="new_conn_from"
            )
        with col2:
            to_location = st.selectbox(
                "To Location",
                options=[l.id for l in locations],
                format_func=lambda x: location_names.get(x, "Unknown"),
                key="new_conn_to"
            )

        col3, col4, col5 = st.columns(3)
        with col3:
            travel_type = st.selectbox(
                "Travel Type",
                options=["walk", "vehicle", "ship", "hyperspace", "teleport", "flight", "swim", "climb"],
                key="new_conn_type"
            )
        with col4:
            travel_time = st.number_input(
                "Travel Time (hours)",
                min_value=0.0,
                value=1.0,
                step=0.5,
                key="new_conn_time"
            )
        with col5:
            bidirectional = st.checkbox("Bidirectional", value=True, key="new_conn_bidir")

        discovered = st.checkbox("Already Discovered", value=True, key="new_conn_discovered")
        description = st.text_input("Description (optional)", key="new_conn_desc")

        if st.form_submit_button("Add Connection", type="primary"):
            if from_location == to_location:
                st.error("Cannot create connection from a location to itself!")
            else:
                # Check if connection already exists
                existing = None
                for conn in connections:
                    if (conn.from_location_id == from_location and conn.to_location_id == to_location) or \
                       (bidirectional and conn.from_location_id == to_location and conn.to_location_id == from_location):
                        existing = conn
                        break

                if existing:
                    st.error("A connection between these locations already exists!")
                else:
                    import uuid
                    with get_session() as session:
                        new_conn = Connection(
                            id=str(uuid.uuid4()),
                            from_location_id=from_location,
                            to_location_id=to_location,
                            travel_type=travel_type,
                            travel_time_hours=travel_time,
                            bidirectional=bidirectional,
                            discovered=discovered,
                            description=description if description else None,
                        )
                        session.add(new_conn)
                        session.commit()
                        st.success(f"Connection added: {location_names[from_location]} -> {location_names[to_location]}")
                        st.rerun()

    st.divider()

    if not connections:
        st.warning("No connections found. Use the form above to add connections.")
        st.stop()

    # Connections table
    import pandas as pd
    conn_data = []
    for conn in connections:
        from_name = location_names.get(conn.from_location_id, "Unknown")
        to_name = location_names.get(conn.to_location_id, "Unknown")
        conn_data.append({
            "From": from_name,
            "To": to_name,
            "Type": conn.travel_type,
            "Time (hrs)": conn.travel_time_hours,
            "Bidirectional": "Yes" if conn.bidirectional else "No",
            "Discovered": "Yes" if conn.discovered else "No",
        })

    if conn_data:
        df = pd.DataFrame(conn_data)
        st.dataframe(df, use_container_width=True)

    # Detailed view
    st.header("Connection Details")
    for conn in connections:
        from_name = location_names.get(conn.from_location_id, "Unknown")
        to_name = location_names.get(conn.to_location_id, "Unknown")
        direction = "↔" if conn.bidirectional else "→"

        with st.expander(f"{from_name} {direction} {to_name} ({conn.travel_type})"):
            col1, col2 = st.columns([2, 1])

            with col1:
                st.markdown(f"**Travel Type:** {conn.travel_type}")
                st.markdown(f"**Travel Time:** {conn.travel_time_hours} hours")
                st.markdown(f"**Bidirectional:** {'Yes' if conn.bidirectional else 'No'}")
                st.markdown(f"**Discovered:** {'Yes' if conn.discovered else 'No'}")

                if conn.requirements:
                    st.markdown("**Requirements:**")
                    for req in conn.requirements:
                        st.markdown(f"- {req}")

            with col2:
                # Edit form
                with st.form(key=f"edit_conn_{conn.id}"):
                    st.markdown("**Edit Connection**")
                    new_travel_time = st.number_input("Travel Time (hours)", value=conn.travel_time_hours, min_value=0.0, step=0.1)
                    new_discovered = st.checkbox("Discovered", value=conn.discovered)

                    if st.form_submit_button("Save Changes"):
                        with get_session() as session:
                            conn_obj = session.get(Connection, conn.id)
                            if conn_obj:
                                conn_obj.travel_time_hours = new_travel_time
                                conn_obj.discovered = new_discovered
                                session.commit()
                                st.success("Connection updated!")
                                st.rerun()

                # Delete button
                if st.button(f"🗑️ Delete Connection", key=f"delete_conn_{conn.id}"):
                    if st.session_state.get(f"confirm_delete_conn_{conn.id}", False):
                        if delete_entity("connection", conn.id):
                            st.success("Connection deleted!")
                            st.rerun()
                        else:
                            st.error("Failed to delete connection")
                    else:
                        st.session_state[f"confirm_delete_conn_{conn.id}"] = True
                        st.warning("Click again to confirm deletion")

elif page == "NPCs":
    st.title("👥 NPCs")

    npcs = get_npcs()
    factions = get_factions()
    locations = get_locations()

    faction_names = {f.id: f.name for f in factions}
    faction_names[None] = "Unaffiliated"
    location_names = {l.id: l.name for l in locations}
    location_names[None] = "Unknown"

    if not npcs:
        st.warning("No NPCs found.")
        st.stop()

    # Filters
    col1, col2, col3 = st.columns(3)
    with col1:
        tier_filter = st.selectbox("Tier", ["All", "major", "minor", "ambient"])
    with col2:
        faction_filter = st.selectbox("Faction", ["All"] + list(faction_names.values()))
    with col3:
        status_filter = st.selectbox("Status", ["All", "alive", "dead", "missing", "imprisoned"])

    # Filter NPCs
    filtered = npcs
    if tier_filter != "All":
        filtered = [n for n in filtered if n.tier.value == tier_filter]
    if faction_filter != "All":
        faction_id = next((fid for fid, fname in faction_names.items() if fname == faction_filter), None)
        filtered = [n for n in filtered if n.faction_id == faction_id]
    if status_filter != "All":
        filtered = [n for n in filtered if n.status == status_filter]

    st.markdown(f"**Showing {len(filtered)} of {len(npcs)} NPCs**")

    # NPC list
    for npc in filtered:
        tier_emoji = {"major": "⭐", "minor": "🔹", "ambient": "·"}.get(npc.tier.value, "")
        with st.expander(f"{tier_emoji} {npc.name} - {npc.profession}"):
            col1, col2 = st.columns([2, 1])

            with col1:
                st.markdown(f"**Species:** {npc.species}, **Age:** {npc.age}")
                st.markdown(f"**Faction:** {faction_names.get(npc.faction_id, 'None')}")
                st.markdown(f"**Location:** {location_names.get(npc.current_location_id, 'Unknown')}")

                if npc.description_physical:
                    st.markdown(f"**Appearance:** {npc.description_physical}")
                if npc.description_personality:
                    st.markdown(f"**Personality:** {npc.description_personality}")
                if npc.voice_pattern:
                    st.markdown(f"**Voice:** {npc.voice_pattern}")

            with col2:
                st.markdown(f"**Mood:** {npc.current_mood}")
                st.markdown(f"**Status:** {npc.status}")

                if npc.goals:
                    st.markdown("**Goals:**")
                    for g in npc.goals:
                        st.markdown(f"- {g}")

                if npc.secrets:
                    st.markdown("**🔒 Secrets:**")
                    for s in npc.secrets:
                        st.markdown(f"- {s}")

            # Edit form
            with st.form(key=f"edit_npc_{npc.id}"):
                st.markdown("**Edit NPC**")
                new_mood = st.text_input("Current Mood", value=npc.current_mood or "neutral")
                new_status = st.selectbox("Status", ["alive", "dead", "missing", "imprisoned"], index=["alive", "dead", "missing", "imprisoned"].index(npc.status) if npc.status in ["alive", "dead", "missing", "imprisoned"] else 0)
                new_location = st.selectbox("Location", options=[None] + [l.id for l in locations], format_func=lambda x: location_names.get(x, "Unknown") if x else "Unknown", index=([None] + [l.id for l in locations]).index(npc.current_location_id) if npc.current_location_id in [l.id for l in locations] else 0)

                if st.form_submit_button("Save Changes"):
                    with get_session() as session:
                        npc_obj = session.get(NPC, npc.id)
                        if npc_obj:
                            npc_obj.current_mood = new_mood
                            npc_obj.status = new_status
                            npc_obj.current_location_id = new_location
                            session.commit()
                            st.success(f"{npc.name} updated!")
                            st.rerun()

            # Delete button
            if st.button(f"🗑️ Delete {npc.name}", key=f"delete_npc_{npc.id}"):
                if st.session_state.get(f"confirm_delete_npc_{npc.id}", False):
                    if delete_entity("npc", npc.id):
                        st.success(f"Deleted {npc.name}")
                        st.rerun()
                    else:
                        st.error("Failed to delete NPC")
                else:
                    st.session_state[f"confirm_delete_npc_{npc.id}"] = True
                    st.warning("Click again to confirm deletion")

elif page == "History":
    st.title("📜 Historical Events")

    events = get_historical_events()

    if not events:
        st.warning("No historical events found.")
        st.stop()

    # Timeline view
    for event in events:
        type_emoji = {"war": "⚔️", "disaster": "💥", "discovery": "🔍", "political": "🏛️",
                     "cultural": "🎭", "religious": "🙏", "economic": "💰"}.get(event.event_type, "📅")

        with st.expander(f"{type_emoji} {event.name} ({event.time_ago})"):
            st.write(event.description)

            col1, col2 = st.columns(2)
            with col1:
                if event.involved_parties:
                    st.markdown(f"**Involved:** {', '.join(event.involved_parties)}")
                if event.key_figures:
                    st.markdown(f"**Key Figures:** {', '.join(event.key_figures)}")
                if event.locations_affected:
                    st.markdown(f"**Locations:** {', '.join(event.locations_affected)}")

            with col2:
                st.markdown(f"**Common Knowledge:** {'Yes' if event.common_knowledge else 'No'}")
                if event.consequences:
                    st.markdown("**Consequences:**")
                    for c in event.consequences:
                        st.markdown(f"- {c}")
                if event.artifacts_left:
                    st.markdown("**Artifacts/Remnants:**")
                    for a in event.artifacts_left:
                        st.markdown(f"- {a}")

elif page == "Events":
    st.title("📅 Runtime Events")

    events = get_runtime_events()
    clock = get_world_clock()

    if clock:
        st.info(f"Current Time: Day {clock.day}, {clock.hour:02d}:00 ({clock.get_time_of_day()})")

    if not events:
        st.warning("No runtime events yet. Events are created during gameplay.")
        st.stop()

    for event in sorted(events, key=lambda e: (e.occurred_day or 0, e.occurred_hour or 0), reverse=True):
        type_emoji = {"macro": "🌍", "meso": "🏘️", "player": "👤"}.get(event.event_type, "📅")
        witnessed = "👁️" if event.player_witnessed else ""

        with st.expander(f"{type_emoji} {event.name} (Day {event.occurred_day}) {witnessed}"):
            st.write(event.description)

            if event.factions_involved:
                st.markdown(f"**Factions:** {', '.join(event.factions_involved)}")
            if event.npcs_involved:
                st.markdown(f"**NPCs:** {', '.join(event.npcs_involved)}")
            if event.consequences:
                st.markdown("**Consequences:**")
                for c in event.consequences:
                    st.markdown(f"- {c}")

elif page == "Raw Data":
    st.title("🗄️ Raw Data Export")

    st.header("Export Options")

    col1, col2 = st.columns(2)

    with col1:
        if st.button("Export World Bible"):
            bible = get_world_bible()
            if bible:
                import json
                data = {
                    "name": bible.name,
                    "genre": bible.genre,
                    "sub_genres": bible.sub_genres,
                    "tone": bible.tone,
                    "themes": bible.themes,
                    "setting_description": bible.setting_description,
                    "current_situation": bible.current_situation,
                    "rules": bible.rules,
                    "technology_level": bible.technology_level,
                    "magic_system": bible.magic_system,
                }
                st.json(data)

        if st.button("Export Factions"):
            factions = get_factions()
            import json
            data = [{
                "name": f.name,
                "ideology": f.ideology,
                "power_level": f.power_level,
                "goals_short": f.goals_short,
                "goals_long": f.goals_long,
                "secrets": f.secrets,
            } for f in factions]
            st.json(data)

    with col2:
        if st.button("Export Locations"):
            locations = get_locations()
            data = [{
                "name": l.name,
                "type": l.type.value,
                "description": l.description,
                "parent_id": l.parent_id,
            } for l in locations]
            st.json(data)

        if st.button("Export NPCs"):
            npcs = get_npcs()
            data = [{
                "name": n.name,
                "tier": n.tier.value,
                "profession": n.profession,
                "description_physical": n.description_physical,
                "goals": n.goals,
                "secrets": n.secrets,
            } for n in npcs]
            st.json(data)

    # Database stats
    st.header("Database Statistics")
    factions = get_factions()
    locations = get_locations()
    npcs = get_npcs()
    hist_events = get_historical_events()
    runtime_events = get_runtime_events()

    import pandas as pd
    stats = pd.DataFrame({
        "Entity": ["Factions", "Locations", "NPCs (Major)", "NPCs (Minor)", "NPCs (Ambient)",
                  "Historical Events", "Runtime Events"],
        "Count": [
            len(factions),
            len(locations),
            len([n for n in npcs if n.tier.value == "major"]),
            len([n for n in npcs if n.tier.value == "minor"]),
            len([n for n in npcs if n.tier.value == "ambient"]),
            len(hist_events),
            len(runtime_events),
        ]
    })
    st.dataframe(stats, use_container_width=True)
