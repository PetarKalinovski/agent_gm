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

from src.models import init_db, get_session, Location, Player, Connection


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


def create_map_figure(
    map_location: Location,
    child_locations: list[Location],
    player_location_id: str | None,
    map_width: int = 1000,
    map_height: int = 800,
) -> go.Figure:
    """Create a Plotly figure for the map.

    Args:
        map_location: The location whose map we're displaying
        child_locations: Children to show as pins
        player_location_id: Current player location ID
        map_width: Width of the map canvas
        map_height: Height of the map canvas

    Returns:
        Plotly Figure object
    """
    fig = go.Figure()

    # Set up the layout
    fig.update_layout(
        title=f"Map: {map_location.name}" if map_location else "World Map",
        xaxis=dict(
            range=[0, 100],
            showgrid=True,
            gridcolor='rgba(128,128,128,0.2)',
            zeroline=False,
            showticklabels=False,
        ),
        yaxis=dict(
            range=[0, 100],
            showgrid=True,
            gridcolor='rgba(128,128,128,0.2)',
            zeroline=False,
            showticklabels=False,
            scaleanchor="x",
            scaleratio=1,
        ),
        width=map_width,
        height=map_height,
        showlegend=True,
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="right",
            x=0.99
        ),
        plot_bgcolor='rgba(20,20,40,0.9)',  # Dark space-like background
        paper_bgcolor='rgba(10,10,20,1)',
        hovermode='closest',
        dragmode='pan',
    )

    # Add background image if available
    if map_location and map_location.map_image_path:
        try:
            img = Image.open(map_location.map_image_path)
            # Convert to base64 for Plotly
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

    # Add location pins
    for loc in child_locations:
        color = loc.pin_color if loc.pin_color != "#3388ff" else type_colors.get(loc.type.value, "#3388ff")
        size = loc.pin_size if loc.pin_size else 15

        # Check if player is here
        is_player_here = loc.id == player_location_id

        # Determine marker symbol
        if is_player_here:
            symbol = "star"
            color = "#FFD700"  # Gold for player
            size = 25
        elif loc.display_type == "area":
            symbol = "square"
        else:
            symbol = "circle"

        # Hover text
        hover_text = f"<b>{loc.name}</b><br>"
        hover_text += f"Type: {loc.type.value}<br>"
        if loc.controlling_faction_id:
            hover_text += f"Controlled by faction<br>"
        if loc.visited:
            hover_text += "✓ Visited<br>"
        if loc.discovered:
            hover_text += "✓ Discovered<br>"
        hover_text += f"<br><i>Click to travel here</i>"

        fig.add_trace(go.Scatter(
            x=[loc.position_x],
            y=[loc.position_y],
            mode='markers+text',
            marker=dict(
                size=size,
                color=color,
                symbol=symbol,
                line=dict(width=2, color='white') if is_player_here else dict(width=1, color='rgba(255,255,255,0.5)'),
            ),
            text=[loc.name],
            textposition="top center",
            textfont=dict(
                size=10,
                color='white',
            ),
            hovertemplate=hover_text + "<extra></extra>",
            name=loc.type.value.title(),
            customdata=[loc.id],  # Store location ID for click handling
            showlegend=False,
        ))

    # Add player marker if they're at this map level but not at a child location
    if player_location_id and map_location and player_location_id == map_location.id:
        fig.add_trace(go.Scatter(
            x=[50],  # Center of map
            y=[50],
            mode='markers+text',
            marker=dict(
                size=30,
                color='#FFD700',
                symbol='star',
                line=dict(width=3, color='white'),
            ),
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

# Get child locations for the current map
if current_map_location:
    with get_session() as session:
        child_locations = session.query(Location).filter(
            Location.parent_id == current_map_location.id
        ).all()

        # Create the map figure
        fig = create_map_figure(
            map_location=current_map_location,
            child_locations=child_locations,
            player_location_id=player_location_id,
            map_width=900,
            map_height=700,
        )

        # Display the map
        selected_point = st.plotly_chart(
            fig,
            use_container_width=True,
            on_select="rerun",
            selection_mode="points",
            key="map_chart"
        )

        # Handle point selection (click on pin)
        if selected_point and selected_point.selection and selected_point.selection.points:
            point = selected_point.selection.points[0]
            if 'customdata' in point and point['customdata']:
                selected_location_id = point['customdata']
                st.session_state.selected_destination = selected_location_id

        # Show location list sidebar
        st.sidebar.subheader(f"Locations in {current_map_location.name}")
        for loc in child_locations:
            icon = "⭐" if loc.id == player_location_id else "📍"
            visited_marker = "✓" if loc.visited else ""

            col1, col2 = st.sidebar.columns([3, 1])
            with col1:
                if st.button(f"{icon} {loc.name} {visited_marker}", key=f"sidebar_{loc.id}"):
                    st.session_state.selected_destination = loc.id
            with col2:
                # Zoom into this location
                with get_session() as session:
                    has_children = session.query(Location).filter(Location.parent_id == loc.id).count() > 0
                if has_children:
                    if st.button("🔍", key=f"zoom_{loc.id}", help="Zoom into this location"):
                        st.session_state.current_map_location_id = loc.id
                        st.rerun()

# Selected destination panel
if st.session_state.selected_destination:
    with get_session() as session:
        dest = session.get(Location, st.session_state.selected_destination)
        if dest:
            st.sidebar.divider()
            st.sidebar.subheader("📌 Selected Location")
            st.sidebar.write(f"**{dest.name}**")
            st.sidebar.write(f"Type: {dest.type.value}")
            st.sidebar.write(f"Position: ({dest.position_x:.1f}, {dest.position_y:.1f})")

            if dest.description:
                st.sidebar.write(dest.description[:200] + "..." if len(dest.description) > 200 else dest.description)

            # Travel button
            if player and dest.id != player_location_id:
                if st.sidebar.button("🚀 Travel Here", type="primary"):
                    if move_player_to(player.id, dest.id):
                        st.success(f"Traveled to {dest.name}!")
                        st.session_state.selected_destination = None
                        st.rerun()
                    else:
                        st.error("Could not travel to this location.")
            elif player and dest.id == player_location_id:
                st.sidebar.info("You are already here!")

            # Zoom in button
            has_children = session.query(Location).filter(Location.parent_id == dest.id).count() > 0
            if has_children:
                if st.sidebar.button("🔍 View Map", type="secondary"):
                    st.session_state.current_map_location_id = dest.id
                    st.session_state.selected_destination = None
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
