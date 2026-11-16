import xml.etree.ElementTree as ET
from pathlib import Path
import umsgpack

from .config import Config, DES_FILE_EXTENSION
from pitct.name_converter import NameConverter

conf = Config.get_instance()

# Function to convert a .wmod file to PiTCT automata and return a control system dictionary.
def convert_wmod(filepath,define_blocked:bool = True):
    """
    Parses a .wmod file and returns a dictionary containing the data for each automaton.

    Args:
        filepath (str): The path to the .wmod file.
        define_blocked (bool, optional): Checks if the automata has blocked events and adds as blocked transitions from state 'blk'. 
    Output:
        control_system (dict): A dictionary with all the automaton names divided between 'Plant', 'Supervisor' and 'Specification', and all the blocked events per automaton in 'Blocked_events'."""

    tree = ET.parse(filepath)
    root = tree.getroot()

    # Handle XML files with or without namespaces.
    def tag(element):
        return element.tag.split('}')[-1]

    def descendants(element, name):
        return [
            child for child in element.iter()
            if tag(child) == name
        ]

    # Map event names to their controllability.
    events = {}

    for event in descendants(root, "EventDecl"):
        name = event.get("Name")
        kind = event.get("Kind")

        if kind == "CONTROLLABLE":
            events[name] = "c"
        elif kind == "UNCONTROLLABLE":
            events[name] = "u"

    # Store data separately for each automaton.
    automata = {}
    control_system = {
        "Plant": [],
        "Supervisor": [],
        "Specification": [],
        "Blocked_events": dict()
    }
    for component in descendants(root, "SimpleComponent"):
        name = component.get("Name")
        kind = component.get("Kind")

        if name is None:
            raise ValueError("Automaton without a Name attribute.")

        if name in automata:
            raise ValueError(f"Duplicate automaton name: {name}")

        if kind is None:
            raise ValueError(f"Automaton {name!r} has no Kind attribute.")

        if kind == "PLANT":
            control_system["Plant"].append(name)
        elif kind == "SUPERVISOR":
            control_system["Supervisor"].append(name)
        elif kind == "SPEC":
            control_system["Specification"].append(name)
        else:
            raise ValueError(f"Automaton {name!r} has an invalid Kind attribute.")

        # Find the Graph element inside the component.
        graph = next(
            (child for child in component if tag(child) == "Graph"),
            None
        )

        if graph is None:
            raise ValueError(
                f"Automaton {name!r} has no Graph element."
            )

        Q = []
        trans = []
        size = 0

        # Extract blockerd events.
        for node_list in graph:
            # Check if there are blocked events declared
            if tag(node_list) == "LabelBlock":
                print(f"Automaton {name} has blocked events.")
                for event in node_list:
                    if tag(event) != "SimpleIdentifier":
                        continue
                    control_system.setdefault("Blocked_events", dict()).setdefault(name, []).append(event.get("Name"))

            elif tag(node_list) != "NodeList":
                continue

            # Extract marked states.
            else:
                for node in node_list:
                    if tag(node) != "SimpleNode":
                        continue

                    state = node.get("Name")
                    size += 1
                    is_marked = any(
                        identifier.get("Name") == ":accepting"
                        for identifier in descendants(
                            node, "SimpleIdentifier"
                        )
                    )
                    if node.get("Initial") == "true":
                        is_initial = state

                    if is_marked and state is not None:
                        Q.append(state)

        # Extract transitions.
        for edge_list in graph:
            if tag(edge_list) != "EdgeList":
                continue

            for edge in edge_list:
                if tag(edge) != "Edge":
                    continue

                source = edge.get("Source")
                target = edge.get("Target")

                for identifier in descendants(
                    edge, "SimpleIdentifier"
                ):
                    event = identifier.get("Name")

                    if event not in events:
                        continue
                    if is_initial == source:
                        trans.insert(0, (
                            source,
                            event,
                            target,
                            events[event]
                        ))
                    else:
                        trans.append((
                            source,
                            event,
                            target,
                            events[event]
                        ))

        # Set Blocked Events as blocked transitions
        if name in control_system["Blocked_events"] and define_blocked:
            size += 1
            for blk_event in control_system["Blocked_events"][name]:
                trans.append((
                    "blk",
                    blk_event,
                    is_initial,
                    events[blk_event]
                ))
                
        automata[name] = {
            "Q": Q,
            "trans": trans,
            "size": size
        }

    return (control_system, automata)


def ctrl_syst_to_xml(ctrl_syst:dict, output_path:str):
    """
    Export all automata declared in a control_system dict to a single XML file.

    Args:
        ctrl_syst (dict): A dictionary with keys 'Plant', 'Supervisor', 'Specification', and 'Blocked_events'.
        output_path (str): Destination XML file path.
    """

    # Create the XML root once.
    root = ET.Element(
        "Automata",
        {
            "name": "Untitled",
            "major": "0",
            "minor": "9"
        }
    )

    automata = []
    for type in ctrl_syst:
        if type == 'Blocked_events': continue
        automata += ctrl_syst[type]

    # Process each automaton separately.
    for automaton in automata:
        automaton_type = [a_type for a_type, value in ctrl_syst.items() if automaton in value][0]

        states = []
        Q = set()
        trans = []
        events = []
        event_control = {}

        # Retrieve data from PiTCT.
        path = Path(conf.SAVE_FOLDER / (automaton + DES_FILE_EXTENSION))
        byte = path.read_bytes()
        data = umsgpack.unpackb(byte)

        # Extract transitions from the data.
        states_data = data["states"]
        for label, state in states_data.items():
            source = str(NameConverter.state_decode(automaton, label, True))
            transition = state["next"]
            if transition is None:
                continue
            for tran in transition:
                event = str(NameConverter.event_decode(tran[0], True))
                destination = str(NameConverter.state_decode(automaton, tran[1], True))
                control = "u" if tran[0] % 2 == 0 else "c"
                trans.append((
                    source,
                    event,
                    destination,
                    control
                ))

        trans = [
            transition
            for transition in trans
            if "blk" not in (transition[0], transition[2])
        ]

        # Infer the initial state from the first transition.
        if not trans:
            raise ValueError(
                f"Automaton {automaton!r} has no transitions. "
                "Its initial state must be provided separately."
            )
        initial_state = trans[0][0]

        # Extract unique events and check for controllability.
        events = list(dict.fromkeys(
            event
            for _, event, _, _ in trans
        ))

        for _, event, _, control in trans:
            if event in event_control and event_control[event] != control:
                raise ValueError(
                    f"Inconsistent controllability for event {event!r} "
                    f"in {automaton!r}."
                )
            event_control[event] = control

        # Extract states and marked states from the data.
        for label, state in states_data.items():
            state_name = str(NameConverter.state_decode(automaton, label, True))
            if state_name != "blk":
                states.append(state_name)

            if state["marked"] and state_name != "blk":
                Q.add(state_name)

        # Include marked states even if they have no transitions.
        for state in Q:
            if state not in states:
                states.append(state)
        for source, _, destination, _ in trans:
            if source not in states:
                states.append(source)
            if destination not in states:
                states.append(destination)

        if initial_state not in states:
            states.append(initial_state)

        # Assign IDs locally to this automaton.
        event_ids = {
            event: str(i)
            for i, event in enumerate(events)
        }
        state_ids = {
            state: str(i)
            for i, state in enumerate(states)
        }

        for source, event, target, control in trans:
            if control not in ("c", "u"):
                raise ValueError(
                    f"Invalid controllability {control!r} "
                    f"for event {event!r}."
                )

            if event not in event_ids:
                raise ValueError(
                    f"Event {event!r} in a transition is missing "
                    f"from pitct.events({automaton!r})."
                )

            if (
                event in event_control
                and event_control[event] != control
            ):
                raise ValueError(
                    f"Inconsistent controllability for event "
                    f"{event!r} in {automaton!r}."
                )

            event_control[event] = control

        # Create the automaton element.
        automaton = ET.SubElement(
            root,
            "Automaton",
            {
                "name": automaton,
                "type": automaton_type
            }
        )

        # 1. Events
        events_element = ET.SubElement(automaton, "Events")

        for event in events:
            attributes = {
                "id": event_ids[event],
                "label": event
            }

            if event_control.get(event) == "u":
                attributes["controllable"] = "false"

            ET.SubElement(
                events_element, "Event", attributes
            )

        # 2. States
        states_element = ET.SubElement(automaton, "States")

        for state in states:
            attributes = {
                "id": state_ids[state],
                "name": str(state)
            }

            if state == initial_state:
                attributes["initial"] = "true"

            if state in Q:
                attributes["accepting"] = "true"

            ET.SubElement(
                states_element, "State", attributes
            )

        # 3. Transitions
        transitions_element = ET.SubElement(
            automaton, "Transitions"
        )

        for source, event, target, control in trans:
            ET.SubElement(
                transitions_element,
                "Transition",
                {
                    "source": state_ids[source],
                    "dest": state_ids[target],
                    "event": event_ids[event]
                }
            )

    # Format and write the complete XML document.
    ET.indent(root, space="\t")

    tree = ET.ElementTree(root)

    tree.write(
        output_path,
        encoding="ISO-8859-1",
        xml_declaration=True
    )