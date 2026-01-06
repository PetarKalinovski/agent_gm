#!/usr/bin/env python3
"""Interactive Map Viewer for the game world.

Displays a hierarchical map with:
- Background map images
- Clickable location pins
- Player avatar at current position
- Click-to-move functionality

Usage:
    streamlit run scripts/map_viewer.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import streamlit as st
import plotly.graph_objects as go
from PIL import Image
import base64
from io import BytesIO

st.set_page_config(
    page_title="World Map",
    page_icon="🗺️",
    layout="wide",
)

from src.models import init_db, get_session, Location, Player, Connection, NPC


# Initialize database
@st.cache_resource
def init_database(db_path: str = "data/sw.db"):
    init_db(db_path)
    return True


def get_location_by_id(location_id: str) -> Location | None:
    """Get a location by ID."""
    with get_session() as session:
        return session.get(Location, location_id)


def get_child_locations(parent_id: str) -> list[Location]:
    """Get all child locations of a parent."""
    with get_session() as session:
        return session.query(Location).filter(Location.parent_id == parent_id).all()


def get_player() -> Player | None:
    """Get the first player (for now, single player)."""
    with get_session() as session:
        return session.query(Player).first()


def get_player_location_hierarchy(player_id: str) -> list[str]:
    """Get the hierarchy of location IDs from root to player's current location."""
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player or not player.current_location_id:
            return []

        hierarchy = []
        current = session.get(Location, player.current_location_id)
        while current:
            hierarchy.insert(0, current.id)
            if current.parent_id:
                current = session.get(Location, current.parent_id)
            else:
                break
        return hierarchy


def get_root_locations() -> list[Location]:
    """Get root-level locations (no parent)."""
    with get_session() as session:
        return session.query(Location).filter(Location.parent_id == None).all()


def move_player_to(player_id: str, destination_id: str) -> bool:
    """Move player to a new location."""
    with get_session() as session:
        player = session.get(Player, player_id)
        destination = session.get(Location, destination_id)

        if not player or not destination:
            return False

        player.current_location_id = destination_id
        destination.visited = True
        destination.discovered = True
        session.commit()
        return True


def update_location_position(location_id: str, x: float, y: float) -> bool:
    """Update a location's position on the map."""
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return False
        location.position_x = x
        location.position_y = y
        session.commit()
        return True


def update_npc_position(npc_id: str, x: float, y: float) -> bool:
    """Update an NPC's position on the map."""
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return False
        npc.position_x = x
        npc.position_y = y
        session.commit()
        return True


def create_map_figure(
    map_location: Location,
    child_locations: list[Location],
    npcs_here: list[NPC],
    player_location_id: str | None,
    map_width: int = 1000,
    map_height: int = 800,
    editable: bool = False,
) -> go.Figure:
    """Create a Plotly figure for the map.

    Args:
        map_location: The location whose map we're displaying
        child_locations: Children to show as pins
        player_location_id: Current player location ID
        map_width: Width of the map canvas
        map_height: Height of the map canvas
        editable: If True, pins can be dragged to new positions

    Returns:
        Plotly Figure object
    """
    fig = go.Figure()

    # Group locations by type for coloring
    type_colors = {
        "galaxy": "#FFD700",      # Gold
        "sector": "#4169E1",      # Royal Blue
        "system": "#32CD32",      # Lime Green
        "planet": "#FF6347",      # Tomato
        "station": "#9370DB",     # Medium Purple
        "city": "#20B2AA",        # Light Sea Green
        "building": "#DEB887",    # Burlywood
        "world": "#FFD700",
        "continent": "#4169E1",
        "kingdom": "#32CD32",
        "province": "#FF6347",
        "town": "#20B2AA",
        "village": "#98FB98",
        "room": "#D2691E",
        "poi": "#FF69B4",
        "district": "#87CEEB",
        "interior": "#BC8F8F",
    }

    shapes = []
    annotations = []

    # Add location pins as draggable shapes
    for loc in child_locations:
        color = loc.pin_color if loc.pin_color != "#3388ff" else type_colors.get(loc.type.value, "#3388ff")
        size = (loc.pin_size if loc.pin_size else 15) / 10  # Scale for shape size

        # Check if player is here
        is_player_here = loc.id == player_location_id

        if is_player_here:
            color = "#FFD700"  # Gold for player
            size = 2.5

        # Create draggable circle shape for each location
        shapes.append(dict(
            type="circle",
            xref="x",
            yref="y",
            x0=loc.position_x - size,
            y0=loc.position_y - size,
            x1=loc.position_x + size,
            y1=loc.position_y + size,
            fillcolor=color,
            opacity=0.8,
            line=dict(color="white", width=2 if is_player_here else 1),
            editable=editable,
            name=loc.id,  # Store location ID in shape name
        ))

        # Add label annotation
        annotations.append(dict(
            x=loc.position_x,
            y=loc.position_y + size + 1.5,
            text=f"{'⭐ ' if is_player_here else ''}{loc.name}",
            showarrow=False,
            font=dict(size=10, color="white"),
            bgcolor="rgba(0,0,0,0.5)",
            borderpad=2,
        ))

    # Set up the layout
    fig.update_layout(
        title=f"Map: {map_location.name}" if map_location else "World Map",
        xaxis=dict(
            range=[0, 100],
            showgrid=True,
            gridcolor='rgba(128,128,128,0.2)',
            zeroline=False,
            showticklabels=True,
            dtick=10,
        ),
        yaxis=dict(
            range=[0, 100],
            showgrid=True,
            gridcolor='rgba(128,128,128,0.2)',
            zeroline=False,
            showticklabels=True,
            dtick=10,
            scaleanchor="x",
            scaleratio=1,
        ),
        width=map_width,
        height=map_height,
        showlegend=False,
        plot_bgcolor='rgba(20,20,40,0.9)',
        paper_bgcolor='rgba(10,10,20,1)',
        hovermode='closest',
        dragmode='pan' if not editable else 'select',
        shapes=shapes,
        annotations=annotations,
    )

    # Add background image if available
    if map_location and map_location.map_image_path:
        try:
            img = Image.open(map_location.map_image_path)
            buffered = BytesIO()
            img.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode()

            fig.add_layout_image(
                dict(
                    source=f"data:image/png;base64,{img_str}",
                    xref="x",
                    yref="y",
                    x=0,
                    y=100,
                    sizex=100,
                    sizey=100,
                    sizing="stretch",
                    opacity=0.8,
                    layer="below"
                )
            )
        except Exception as e:
            st.warning(f"Could not load map image: {e}")

    # Also add scatter traces for click detection (invisible, just for selection)
    for loc in child_locations:
        is_player_here = loc.id == player_location_id
        fig.add_trace(go.Scatter(
            x=[loc.position_x],
            y=[loc.position_y],
            mode='markers',
            marker=dict(size=30, opacity=0),  # Invisible but clickable
            customdata=[f"loc:{loc.id}"],
            hovertemplate=f"<b>{loc.name}</b><br>{'⭐ You are here<br>' if is_player_here else ''}Click to select<extra></extra>",
            showlegend=False,
        ))

    # Add NPC markers as visible shapes with different style
    npc_status_colors = {
        "alive": "#00FF7F",      # Spring Green
        "dead": "#808080",       # Gray
        "missing": "#FFD700",    # Gold
        "imprisoned": "#FF4500", # Orange Red
    }

    for npc in npcs_here:
        color = npc_status_colors.get(npc.status, "#00FF7F")
        size = 1.2  # Smaller than location pins

        # NPC shape (diamond)
        shapes.append(dict(
            type="circle",
            xref="x",
            yref="y",
            x0=npc.position_x - size,
            y0=npc.position_y - size,
            x1=npc.position_x + size,
            y1=npc.position_y + size,
            fillcolor=color,
            opacity=0.9,
            line=dict(color="black", width=2),
            name=f"npc:{npc.id}",
        ))

        # NPC label
        annotations.append(dict(
            x=npc.position_x,
            y=npc.position_y - size - 1.5,
            text=f"👤 {npc.name}",
            showarrow=False,
            font=dict(size=9, color="white"),
            bgcolor="rgba(0,100,0,0.7)",
            borderpad=2,
        ))

        # Invisible scatter for click detection
        fig.add_trace(go.Scatter(
            x=[npc.position_x],
            y=[npc.position_y],
            mode='markers',
            marker=dict(size=25, opacity=0),
            customdata=[f"npc:{npc.id}"],
            hovertemplate=f"<b>👤 {npc.name}</b><br>{npc.profession}<br>Status: {npc.status}<extra></extra>",
            showlegend=False,
        ))

    # Update layout with new shapes/annotations
    fig.update_layout(shapes=shapes, annotations=annotations)

    # Add player marker if at map level
    if player_location_id and map_location and player_location_id == map_location.id:
        fig.add_trace(go.Scatter(
            x=[50], y=[50],
            mode='markers+text',
            marker=dict(size=30, color='#FFD700', symbol='star', line=dict(width=3, color='white')),
            text=["YOU ARE HERE"],
            textposition="top center",
            textfont=dict(size=12, color='#FFD700'),
            hovertemplate="<b>Your Current Location</b><extra></extra>",
            name="Player",
            showlegend=True,
        ))

    return fig


def render_breadcrumb(location_hierarchy: list[tuple[str, str]]) -> str | None:
    """Render navigation breadcrumb and return clicked location ID."""
    if not location_hierarchy:
        return None

    cols = st.columns(len(location_hierarchy) + 1)

    clicked_id = None
    for i, (loc_id, loc_name) in enumerate(location_hierarchy):
        with cols[i]:
            if st.button(f"📍 {loc_name}", key=f"breadcrumb_{loc_id}"):
                clicked_id = loc_id
        if i < len(location_hierarchy) - 1:
            st.write(" → ", end="")

    return clicked_id


# Initialize
init_database()

# Session state initialization
if 'current_map_location_id' not in st.session_state:
    st.session_state.current_map_location_id = None
if 'selected_destination' not in st.session_state:
    st.session_state.selected_destination = None
if 'selected_npc' not in st.session_state:
    st.session_state.selected_npc = None

# Clean up old session state from previous version
for key in ['edit_mode', 'moving_location_id']:
    if key in st.session_state:
        del st.session_state[key]

# Get player info
player = get_player()
player_location_id = player.current_location_id if player else None

# Sidebar controls
st.sidebar.title("🗺️ Map Controls")

# Get root locations
root_locations = get_root_locations()

if not root_locations:
    st.error("No locations found! Generate a world first.")
    st.stop()

# Select which map to view
if st.session_state.current_map_location_id is None:
    # Start at root level
    if len(root_locations) == 1:
        st.session_state.current_map_location_id = root_locations[0].id
    else:
        st.sidebar.write("Multiple root locations found. Select one:")
        for loc in root_locations:
            if st.sidebar.button(loc.name, key=f"root_{loc.id}"):
                st.session_state.current_map_location_id = loc.id
                st.rerun()

# Get current map location
current_map_location = None
if st.session_state.current_map_location_id:
    with get_session() as session:
        current_map_location = session.get(Location, st.session_state.current_map_location_id)

# Build breadcrumb
breadcrumb_data = []
if current_map_location:
    with get_session() as session:
        loc = session.get(Location, current_map_location.id)
        temp_hierarchy = []
        while loc:
            temp_hierarchy.insert(0, (loc.id, loc.name))
            if loc.parent_id:
                loc = session.get(Location, loc.parent_id)
            else:
                break
        breadcrumb_data = temp_hierarchy

# Main content
st.title("🗺️ World Map")

# Breadcrumb navigation
if breadcrumb_data:
    st.write("**Navigation:**")
    nav_cols = st.columns(len(breadcrumb_data))
    for i, (loc_id, loc_name) in enumerate(breadcrumb_data):
        with nav_cols[i]:
            prefix = "🌌 " if i == 0 else "→ "
            if st.button(f"{prefix}{loc_name}", key=f"nav_{loc_id}"):
                st.session_state.current_map_location_id = loc_id
                st.rerun()

st.divider()

# Get child locations and NPCs for the current map
if current_map_location:
    with get_session() as session:
        child_locations = session.query(Location).filter(
            Location.parent_id == current_map_location.id
        ).all()

        # Get NPCs at this location
        npcs_at_location = session.query(NPC).filter(
            NPC.current_location_id == current_map_location.id
        ).all()

        # Create the map figure (fast, no edit mode complexity)
        fig = create_map_figure(
            map_location=current_map_location,
            child_locations=child_locations,
            npcs_here=npcs_at_location,
            player_location_id=player_location_id,
            map_width=900,
            map_height=700,
            editable=False,
        )

        # Display map with standard plotly_chart (fast)
        st.plotly_chart(fig, use_container_width=True, key="map_chart")

        # Show location list sidebar
        st.sidebar.subheader(f"📍 Locations ({len(child_locations)})")
        for loc in child_locations:
            icon = "⭐" if loc.id == player_location_id else "📍"
            visited_marker = "✓" if loc.visited else ""

            col1, col2 = st.sidebar.columns([3, 1])
            with col1:
                if st.button(f"{icon} {loc.name} {visited_marker}", key=f"sidebar_{loc.id}"):
                    st.session_state.selected_destination = loc.id
                    st.session_state.selected_npc = None  # Deselect NPC
            with col2:
                # Always allow zooming into any location
                if st.button("🔍", key=f"zoom_{loc.id}", help="View this location's map"):
                    st.session_state.current_map_location_id = loc.id
                    st.rerun()

        # Show NPCs at this location
        if npcs_at_location:
            st.sidebar.subheader(f"👤 Characters ({len(npcs_at_location)})")
            for npc in npcs_at_location:
                status_icon = {"alive": "🟢", "dead": "💀", "missing": "❓", "imprisoned": "⛓️"}.get(npc.status, "🟢")
                if st.sidebar.button(f"{status_icon} {npc.name} - {npc.profession}", key=f"npc_{npc.id}"):
                    st.session_state.selected_npc = npc.id
                    st.session_state.selected_destination = None  # Deselect location

# Selected destination panel
if st.session_state.selected_destination:
    with get_session() as session:
        dest = session.get(Location, st.session_state.selected_destination)
        if dest:
            st.sidebar.divider()
            st.sidebar.subheader("📌 Selected Location")
            st.sidebar.write(f"**{dest.name}**")
            st.sidebar.write(f"Type: {dest.type.value}")

            if dest.description:
                st.sidebar.write(dest.description[:150] + "..." if len(dest.description) > 150 else dest.description)

            # NPCs at this location
            from src.models import NPC
            npcs_here = session.query(NPC).filter(NPC.current_location_id == dest.id).all()
            if npcs_here:
                st.sidebar.markdown("**NPCs Here:**")
                for npc in npcs_here:
                    status_icon = {"alive": "🟢", "dead": "💀", "missing": "❓", "imprisoned": "⛓️"}.get(npc.status, "")
                    st.sidebar.write(f"{status_icon} {npc.name} - {npc.profession}")

            # Position editing with sliders
            st.sidebar.markdown("**Move Pin Position:**")
            new_x = st.sidebar.slider(
                "X Position",
                min_value=0,
                max_value=100,
                value=int(dest.position_x),
                key=f"slider_x_{dest.id}"
            )
            new_y = st.sidebar.slider(
                "Y Position",
                min_value=0,
                max_value=100,
                value=int(dest.position_y),
                key=f"slider_y_{dest.id}"
            )

            # Auto-save when sliders change
            if int(new_x) != int(dest.position_x) or int(new_y) != int(dest.position_y):
                if update_location_position(dest.id, float(new_x), float(new_y)):
                    st.sidebar.success(f"Moved to ({new_x}, {new_y})")
                    st.rerun()

            # Action buttons
            col_btn1, col_btn2 = st.sidebar.columns(2)

            # Travel button
            with col_btn1:
                if player and dest.id != player_location_id:
                    if st.button("🚀 Travel", type="primary", key="travel_btn"):
                        if move_player_to(player.id, dest.id):
                            st.session_state.selected_destination = None
                            st.rerun()
                elif player and dest.id == player_location_id:
                    st.write("📍 Here")

            # Zoom in button - always available
            with col_btn2:
                if st.button("🔍 Zoom", key="zoom_btn"):
                    st.session_state.current_map_location_id = dest.id
                    st.session_state.selected_destination = None
                    st.rerun()

            # Deselect button
            if st.sidebar.button("✖ Deselect", key="deselect_btn"):
                st.session_state.selected_destination = None
                st.rerun()

# Selected NPC panel
if st.session_state.selected_npc:
    with get_session() as session:
        npc = session.get(NPC, st.session_state.selected_npc)
        if npc:
            st.sidebar.divider()
            st.sidebar.subheader("👤 Selected Character")
            status_icon = {"alive": "🟢", "dead": "💀", "missing": "❓", "imprisoned": "⛓️"}.get(npc.status, "🟢")
            st.sidebar.write(f"**{status_icon} {npc.name}**")
            st.sidebar.write(f"Profession: {npc.profession}")
            st.sidebar.write(f"Status: {npc.status} | Mood: {npc.current_mood}")

            if npc.description_physical:
                st.sidebar.write(npc.description_physical[:100] + "..." if len(npc.description_physical) > 100 else npc.description_physical)

            # Position editing with sliders
            st.sidebar.markdown("**Move Character Position:**")
            new_x = st.sidebar.slider(
                "X Position",
                min_value=0,
                max_value=100,
                value=int(npc.position_x),
                key=f"npc_slider_x_{npc.id}"
            )
            new_y = st.sidebar.slider(
                "Y Position",
                min_value=0,
                max_value=100,
                value=int(npc.position_y),
                key=f"npc_slider_y_{npc.id}"
            )

            # Auto-save when sliders change
            if int(new_x) != int(npc.position_x) or int(new_y) != int(npc.position_y):
                if update_npc_position(npc.id, float(new_x), float(new_y)):
                    st.sidebar.success(f"Moved to ({new_x}, {new_y})")
                    st.rerun()

            # Deselect button
            if st.sidebar.button("✖ Deselect", key="deselect_npc_btn"):
                st.session_state.selected_npc = None
                st.rerun()

# Player info
st.sidebar.divider()
st.sidebar.subheader("👤 Player")
if player:
    st.sidebar.write(f"Name: {player.name}")
    if player_location_id:
        with get_session() as session:
            player_loc = session.get(Location, player_location_id)
            if player_loc:
                st.sidebar.write(f"Location: {player_loc.name}")
else:
    st.sidebar.warning("No player found. Create one first.")

# Zoom out button
if current_map_location and current_map_location.parent_id:
    if st.sidebar.button("⬆️ Zoom Out", type="secondary"):
        st.session_state.current_map_location_id = current_map_location.parent_id
        st.session_state.selected_destination = None
        st.rerun()
