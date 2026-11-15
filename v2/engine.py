"""Version 2 game engine: structured game state + rule-based action validation.

Python is the single source of truth. Every player action is validated here and the
state only changes when an action succeeds. The LLM never sees the secret codes and
can never change the state - it only narrates the facts this module returns.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "room_config.json"

VERBS = (
    "look", "examine", "take", "open", "use", "enter_code",
    "inventory", "hint", "help", "cheat", "unknown",
)


@dataclass
class Action:
    """A structured player action produced by the parser."""
    verb: str
    target: str | None = None   # object/item the action is aimed at (raw text or id)
    item: str | None = None     # item being used (raw text or id)
    code: str | None = None     # 4-digit code, extracted by regex (never by the LLM)
    raw: str = ""


@dataclass
class Result:
    """The outcome Python decided. The narrator may only describe these facts."""
    success: bool
    event: str
    facts: list[str] = field(default_factory=list)
    escaped: bool = False


class GameEngine:
    def __init__(self, config_path: Path | str = CONFIG_PATH):
        with open(config_path, encoding="utf-8") as f:
            self.config = json.load(f)
        self.objects = self.config["objects"]
        self.items = self.config["items"]
        self.secret_codes = {
            o["lock"]["code"] for o in self.objects.values()
            if o.get("lock", {}).get("type") == "code"
        }

    # ------------------------------------------------------------------ state
    def new_state(self) -> dict:
        """Fresh game state. Everything that can change lives in this dict."""
        return {
            "objects": {
                oid: {
                    "visible": o.get("visible", True),
                    "locked": o.get("locked", False),
                    "contains": list(o.get("contains", [])),
                }
                for oid, o in self.objects.items()
            },
            "inventory": [],
            "solved": [],
            "discovered_clues": [],
            "door_locked": True,
            "escaped": False,
            "turn": 0,
            "failed_attempts": 0,
            "action_log": [],
        }

    def public_state(self, state: dict) -> dict:
        """State without secrets - safe to show in the UI or send to the LLM."""
        visible = {}
        for oid, s in state["objects"].items():
            if not s["visible"]:
                continue
            info = {"name": self.objects[oid]["name"]}
            if "lock" in self.objects[oid]:
                info["status"] = "locked" if s["locked"] else "open"
            visible[oid] = info
        return {
            "visible_objects": visible,
            "inventory": [self.items[i]["name"] for i in state["inventory"]],
            "solved_puzzles": list(state["solved"]),
            "discovered_clues": [self.config["clues"][c] for c in state["discovered_clues"]],
            "door": "locked" if state["door_locked"] else "open",
            "escaped": state["escaped"],
        }

    # -------------------------------------------------------------- resolving
    def _alias_table(self) -> list[tuple[str, str, str]]:
        rows = [(a.lower(), "object", oid) for oid, o in self.objects.items() for a in o["aliases"] + [oid]]
        rows += [(a.lower(), "item", iid) for iid, i in self.items.items() for a in i["aliases"] + [iid]]
        return sorted(rows, key=lambda r: -len(r[0]))  # longest alias first

    def resolve(self, text: str | None) -> tuple[str, str] | None:
        """Map free text ('the old drawer') to ('object', 'drawer'). None if unknown."""
        if not text:
            return None
        text = text.lower().replace("_", " ")
        for alias, kind, ident in self._alias_table():
            if re.search(rf"\b{re.escape(alias.replace('_', ' '))}\b", text):
                return kind, ident
        return None

    def _item_location(self, state: dict, iid: str) -> str | None:
        """Object id whose (unlocked) container currently holds the item."""
        for oid, s in state["objects"].items():
            if iid in s["contains"] and s["visible"] and not s["locked"]:
                return oid
        return None

    # --------------------------------------------------------------- dispatch
    def handle(self, state: dict, action: Action) -> Result:
        """Validate and apply one action. On any failure the state is left untouched."""
        if state["escaped"]:
            return Result(False, "game_over", ["The player has already escaped. The game is over."], True)

        snapshot = copy.deepcopy(state)
        handler = getattr(self, f"_do_{action.verb}", self._do_unknown)
        try:
            result = handler(state, action)
        except Exception as exc:  # never let a bug corrupt the game
            state.clear()
            state.update(snapshot)
            result = Result(False, "error", [f"Internal error handled safely: {exc}"])

        if not result.success and result.event not in ("look", "inventory", "hint", "help"):
            # Defensive: failed actions must never change the game.
            preserved_log, preserved_fail = snapshot["action_log"], snapshot["failed_attempts"]
            state.clear()
            state.update(snapshot)
            state["action_log"], state["failed_attempts"] = preserved_log, preserved_fail
            if result.event == "wrong_code":
                state["failed_attempts"] += 1

        state["turn"] += 1
        state["action_log"].append({
            "turn": state["turn"], "input": action.raw, "verb": action.verb,
            "target": action.target, "success": result.success, "event": result.event,
        })
        return result

    # ---------------------------------------------------------------- verbs
    def _do_look(self, state, action):
        names = [self.objects[o]["name"] for o, s in state["objects"].items() if s["visible"]]
        return Result(True, "look", [self.config["room_description"],
                                     "Visible things: " + ", ".join(names) + "."])

    def _do_help(self, state, action):
        return Result(True, "help", [
            "Possible actions: look around, examine <thing>, take <item>, open <thing>, "
            "use <item> on <thing>, enter <code> on <thing>, inventory, hint."
        ])

    def _do_inventory(self, state, action):
        inv = [self.items[i]["name"] for i in state["inventory"]]
        return Result(True, "inventory", ["The player is carrying: " + (", ".join(inv) if inv else "nothing") + "."])

    def _do_hint(self, state, action):
        for h in self.config["hints"]:
            if not self._condition_met(state, h["until"]):
                return Result(True, "hint", ["Hint: " + h["text"]])
        return Result(True, "hint", ["Hint: You have everything you need. Head for the door."])

    def _condition_met(self, state, cond: str) -> bool:
        if cond.startswith("has:"):
            return cond[4:] in state["inventory"]
        return cond in state["solved"]

    def _do_cheat(self, state, action):
        progress = f"{len(state['solved'])} of 5 puzzles actually solved"
        return Result(False, "rule_violation_refused", [
            "The player tried to bypass the rules (claimed authority, claimed progress, or asked for secrets).",
            "This has no effect. The room only responds to actions taken inside the game.",
            f"True progress according to the game state: {progress}. Door: {'locked' if state['door_locked'] else 'open'}.",
        ])

    def _do_unknown(self, state, action):
        return Result(False, "not_understood", [
            "The Game Master did not understand the action. Suggest examining objects, taking items or typing 'hint'."
        ])

    def _do_examine(self, state, action):
        if not action.target:
            return self._do_look(state, action)
        ref = self.resolve(action.target)
        if ref is None:
            return self._no_such(action.target)
        kind, ident = ref

        if kind == "item":
            if ident in state["inventory"]:
                return Result(True, "examine_item", [self.items[ident]["description"]])
            loc = self._item_location(state, ident)
            if loc:
                return Result(True, "examine_item", [
                    f"Inside the {self.objects[loc]['name']}: {self.items[ident]['description']}"])
            return self._no_such(action.target)

        obj, s = self.objects[ident], state["objects"][ident]
        if not s["visible"]:
            return self._no_such(action.target)

        facts = [self._describe(ident, state)]
        for revealed in obj.get("reveals", []):
            if not state["objects"][revealed]["visible"]:
                state["objects"][revealed]["visible"] = True
                facts.append(f"New discovery: the {self.objects[revealed]['name']} can now be examined.")
        if "on_examine" in obj:
            self._solve(state, obj["on_examine"]["solve"], obj["on_examine"].get("clue"))
        return Result(True, "examine", facts)

    def _describe(self, oid: str, state: dict) -> str:
        obj, s = self.objects[oid], state["objects"][oid]
        if "lock" in obj:
            text = obj["description_locked"] if s["locked"] else obj["description_unlocked"]
            if not s["locked"]:
                text += " " + self._contents_text(oid, state)
        else:
            text = obj["description"]
        return text.strip()

    def _contents_text(self, oid: str, state: dict) -> str:
        items = [self.items[i]["name"] for i in state["objects"][oid]["contains"]]
        return ("Inside you see: " + ", ".join(items) + ".") if items else "It is empty."

    def _do_take(self, state, action):
        ref = self.resolve(action.target)
        if ref is None:
            return self._no_such(action.target or "that")
        kind, ident = ref
        if kind == "object":
            return Result(False, "cannot_take", [f"The {self.objects[ident]['name']} cannot be picked up."])
        if ident in state["inventory"]:
            return Result(False, "already_have", [f"The player already has the {self.items[ident]['name']}."])
        loc = self._item_location(state, ident)
        if loc is None:
            return Result(False, "not_reachable", [
                f"There is no {action.target} within reach. (It has not been found yet.)"])
        state["objects"][loc]["contains"].remove(ident)
        state["inventory"].append(ident)
        return Result(True, "take", [f"The player takes the {self.items[ident]['name']} from the {self.objects[loc]['name']}."])

    def _do_open(self, state, action):
        if action.code:
            return self._do_enter_code(state, action)
        if action.item:
            return self._do_use(state, action)
        ref = self.resolve(action.target)
        if ref is None or ref[0] != "object" or not state["objects"][ref[1]]["visible"]:
            return self._no_such(action.target or "that")
        oid = ref[1]
        obj, s = self.objects[oid], state["objects"][oid]
        if "lock" not in obj:  # e.g. "open the red books" -> treat as examining
            return self._do_examine(state, Action("examine", target=oid, raw=action.raw))
        if s["locked"]:
            need = "a 4-digit code" if obj["lock"]["type"] == "code" else "a key"
            return Result(False, "locked", [obj["description_locked"], f"It needs {need}."])
        return Result(True, "open", [self._describe(oid, state)])

    def _do_use(self, state, action):
        item_ref = self.resolve(action.item)
        if item_ref is None or item_ref[0] != "item":
            return Result(False, "no_such_item", [
                f"The player does not have any '{action.item or 'item'}'. Nothing like that exists in their inventory."])
        iid = item_ref[1]
        if iid not in state["inventory"]:
            return Result(False, "item_not_in_inventory", [
                f"The player tries to use a {self.items[iid]['name']}, but they are not carrying one."])

        tgt = self.resolve(action.target)
        if tgt is None or tgt[0] != "object" or not state["objects"][tgt[1]]["visible"]:
            return Result(False, "no_target", [f"Use the {self.items[iid]['name']} on what? That target is not in the room."])
        oid = tgt[1]
        obj, s = self.objects[oid], state["objects"][oid]

        lock = obj.get("lock", {})
        if lock.get("type") == "item" and lock.get("item") == iid:
            if not s["locked"]:
                return Result(False, "already_open", [f"The {obj['name']} is already open."])
            s["locked"] = False
            self._solve(state, lock["solve"])
            return Result(True, "unlock", [f"The {self.items[iid]['name']} fits. The {obj['name']} unlocks.",
                                           self._describe(oid, state)])

        reveal = obj.get("use_reveal")
        if reveal and reveal["item"] == iid:
            already = reveal["solve"] in state["solved"]
            self._solve(state, reveal["solve"], reveal.get("clue"))
            return Result(True, "reveal", [reveal["text"]] + (["(The player had already seen this.)"] if already else []))

        return Result(False, "no_effect", [f"Using the {self.items[iid]['name']} on the {obj['name']} does nothing."])

    def _do_enter_code(self, state, action):
        if not action.code:
            return Result(False, "no_code", ["The player needs to say which 4-digit code to enter."])
        oid = self._code_target(state, action.target)
        if oid is None:
            return Result(False, "no_target", ["There is nothing here that accepts that code."])
        obj, s = self.objects[oid], state["objects"][oid]
        lock = obj["lock"]
        if not s["locked"]:
            return Result(False, "already_open", [f"The {obj['name']} is already open."])

        # Correct code only counts once the clue has really been discovered in-game.
        # A code obtained by guessing or by tricking the LLM does not work.
        if action.code == lock["code"] and lock.get("requires", "") in state["solved"] + [""]:
            s["locked"] = False
            self._solve(state, lock["solve"])
            if obj.get("final"):
                state["door_locked"] = False
                state["escaped"] = True
                return Result(True, "escaped", [
                    f"The keypad flashes green after entering {action.code}. The door swings open.",
                    "The player has escaped The Locked Library. The game is won."], escaped=True)
            return Result(True, "unlock", [f"Code {action.code} is accepted. The {obj['name']} unlocks.",
                                           self._describe(oid, state)])
        return Result(False, "wrong_code", [f"The player enters {action.code} on the {obj['name']}. "
                                            "It buzzes and stays locked. Wrong code."])

    def _code_target(self, state, target_text):
        ref = self.resolve(target_text)
        if ref and ref[0] == "object" and self.objects[ref[1]].get("lock", {}).get("type") == "code":
            return ref[1]
        if ref:  # target named but it does not take codes
            return None
        # No target given: first still-locked code lock (drawer before door)
        for oid, o in self.objects.items():
            if o.get("lock", {}).get("type") == "code" and state["objects"][oid]["locked"]:
                return oid
        return None

    # ---------------------------------------------------------------- helpers
    def _solve(self, state, puzzle: str, clue: str | None = None):
        if puzzle not in state["solved"]:
            state["solved"].append(puzzle)
        if clue and clue not in state["discovered_clues"]:
            state["discovered_clues"].append(clue)

    def _no_such(self, text):
        return Result(False, "no_such_object", [
            f"There is no '{text}' in this room (or the player has not found it). Do not invent it."])
