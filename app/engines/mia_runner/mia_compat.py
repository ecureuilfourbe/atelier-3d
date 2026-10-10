"""Compatibilité Make-It-Animatable (MIT, https://github.com/jasongzy/Make-It-Animatable).

Le module upstream `util/dataset_mixamo.py` charge à l'import le squelette `data/Mixamo/bones.fbx`
(jeu de données à accès protégé) via `bpy`, uniquement pour obtenir l'arbre cinématique.
L'inférence n'utilise de cet arbre que les relations parent/enfant et la profondeur des articulations
(masques d'attention), indépendamment de l'ordre des enfants. Ce module reconstruit cet arbre à partir
de la hiérarchie Mixamo standard, et reprend à l'identique (licence MIT, © Zhiyang Guo et al.) les
listes d'articulations et les fonctions géométriques nécessaires, sans dépendance à Blender.

`install()` enregistre ce module sous le nom `util.dataset_mixamo` avant l'import de `model.py`.
"""
from __future__ import annotations

import sys
import types
from collections import OrderedDict
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType
from typing import Iterator

import torch
import torch.nn.functional as F

MIXAMO_PREFIX = "mixamorig:"
_FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def _side_chain(side: str) -> list[str]:
    names = [f"{side}Shoulder", f"{side}Arm", f"{side}ForeArm", f"{side}Hand"]
    for f in _FINGERS:
        names += [f"{side}Hand{f}{i}" for i in (1, 2, 3)]
    return names


# Ordre identique à MIXAMO_JOINTS upstream (52 articulations, doigts sans phalange « 4 »).
MIXAMO_JOINTS = tuple(
    MIXAMO_PREFIX + n
    for n in (
        ["Hips", "Spine", "Spine1", "Spine2", "Neck", "Head"]
        + _side_chain("Left")
        + _side_chain("Right")
        + ["LeftUpLeg", "LeftLeg", "LeftFoot", "LeftToeBase", "RightUpLeg", "RightLeg", "RightFoot", "RightToeBase"]
    )
)
assert len(MIXAMO_JOINTS) == 52
JOINTS_NUM = len(MIXAMO_JOINTS)
BONES_IDX_DICT = MappingProxyType(OrderedDict({name: i for i, name in enumerate(MIXAMO_JOINTS)}))


def mixamo_parent(name: str) -> str | None:
    """Parent dans la hiérarchie Mixamo standard (noms sans préfixe)."""
    chain = {"Spine": "Hips", "Spine1": "Spine", "Spine2": "Spine1", "Neck": "Spine2", "Head": "Neck"}
    if name == "Hips":
        return None
    if name in chain:
        return chain[name]
    for side in ("Left", "Right"):
        if name == f"{side}Shoulder":
            return "Spine2"
        if name == f"{side}Arm":
            return f"{side}Shoulder"
        if name == f"{side}ForeArm":
            return f"{side}Arm"
        if name == f"{side}Hand":
            return f"{side}ForeArm"
        for f in _FINGERS:
            if name == f"{side}Hand{f}1":
                return f"{side}Hand"
            for i in (2, 3):
                if name == f"{side}Hand{f}{i}":
                    return f"{side}Hand{f}{i - 1}"
        if name == f"{side}UpLeg":
            return "Hips"
        if name == f"{side}Leg":
            return f"{side}UpLeg"
        if name == f"{side}Foot":
            return f"{side}Leg"
        if name == f"{side}ToeBase":
            return f"{side}Foot"
    raise KeyError(name)


PARENT_INDICES = tuple(
    -1 if mixamo_parent(n[len(MIXAMO_PREFIX):]) is None else BONES_IDX_DICT[MIXAMO_PREFIX + mixamo_parent(n[len(MIXAMO_PREFIX):])]
    for n in MIXAMO_JOINTS
)


@dataclass(frozen=True)
class Joint:
    """Copie de la classe `Joint` upstream (MIT)."""

    name: str
    index: int
    parent: "Joint | None"
    children: list
    template_joints: tuple

    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"

    def __iter__(self) -> Iterator["Joint"]:
        yield self
        for child in self.children:
            yield from child

    @cached_property
    def children_recursive(self) -> list:
        out = []
        for child in self.children:
            out.append(child)
            out.extend(child.children_recursive)
        return out

    def __len__(self):
        return len(self.children_recursive) + 1

    @cached_property
    def parent_recursive(self) -> list:
        if self.parent is None:
            return []
        return [self.parent] + self.parent.parent_recursive

    @cached_property
    def joints_list(self) -> list:
        lst = [None] * len(self)
        for j in self:
            lst[j.index] = j
        assert None not in lst
        return lst

    @cached_property
    def parent_indices(self) -> list[int]:
        return [-1 if j.parent is None else j.parent.index for j in self.joints_list]

    @cached_property
    def tree_levels(self) -> dict:
        levels = {0: [self]}
        for child in self.children:
            for lvl, nodes in child.tree_levels.items():
                levels.setdefault(lvl + 1, []).extend(nodes)
        return levels

    @cached_property
    def tree_levels_name(self) -> dict:
        return {lvl: [j.name for j in level] for lvl, level in self.tree_levels.items()}

    @cached_property
    def tree_levels_index(self) -> dict:
        return {lvl: [j.index for j in level] for lvl, level in self.tree_levels.items()}

    @cached_property
    def tree_levels_mask(self):
        return [[j in self.tree_levels_name[lvl] for j in self.template_joints] for lvl in range(len(self.tree_levels_name))]


def build_kinematic_tree() -> Joint:
    template = tuple(BONES_IDX_DICT.keys())
    nodes: dict[str, Joint] = {}

    def make(name: str, parent: Joint | None) -> Joint:
        j = Joint(name, index=BONES_IDX_DICT[name], parent=parent, children=[], template_joints=template)
        nodes[name] = j
        for child in MIXAMO_JOINTS:
            p = mixamo_parent(child[len(MIXAMO_PREFIX):])
            if p is not None and MIXAMO_PREFIX + p == name:
                j.children.append(make(child, j))
        return j

    root = make(MIXAMO_PREFIX + "Hips", None)
    assert len(root) == JOINTS_NUM
    return root


KINEMATIC_TREE = build_kinematic_tree()
TEMPLATE_PATH = None  # non utilisé par l'inférence


def _apply_transform(xyz, transform):
    ones = torch.ones_like(xyz[..., 0:1])
    h = torch.cat((xyz, ones), -1)
    return torch.einsum("...ij,...j->...i", transform, h)[..., :3]


def get_hips_normal(hips, rightupleg, leftupleg):
    return torch.cross(rightupleg - hips, leftupleg - hips, dim=-1)


def get_plane_transform(origin, normal, tangent_ends):
    """Copie upstream (MIT) : place `origin` à l'origine, `normal` vers +Z et la tangente vers +X."""
    B = normal.shape[0]
    normal = F.normalize(normal, dim=-1)
    target = F.normalize(torch.tensor([0, 0, 1]).to(normal), dim=-1).unsqueeze(0).repeat(B, 1)
    cross = torch.cross(normal, target, dim=-1)
    dot = torch.sum(normal * target, dim=-1, keepdim=True)
    cross_norm = cross.norm(dim=-1, keepdim=True)
    sk = torch.zeros((B, 3, 3)).to(normal)
    sk[:, 0, 1] = -cross[:, 2]
    sk[:, 0, 2] = cross[:, 1]
    sk[:, 1, 0] = cross[:, 2]
    sk[:, 1, 2] = -cross[:, 0]
    sk[:, 2, 0] = -cross[:, 1]
    sk[:, 2, 1] = cross[:, 0]
    rotation = torch.eye(3).to(normal).unsqueeze(0).repeat(B, 1, 1) + sk + sk @ sk * ((1 - dot) / (cross_norm**2)).unsqueeze(-1)
    rot = torch.eye(4).to(normal).unsqueeze(0).repeat(B, 1, 1)
    rot[:, :3, :3] = rotation
    tr = torch.eye(4).to(normal).unsqueeze(0).repeat(B, 1, 1)
    tr[:, :3, 3] = -origin
    matrix = rot @ tr
    ends = _apply_transform(tangent_ends, matrix.unsqueeze(1).expand(-1, 2, -1, -1))
    tangent = ends[:, 1] - ends[:, 0]
    angles = torch.atan2(tangent[:, 1], tangent[:, 0])
    c, s = torch.cos(-angles), torch.sin(-angles)
    rot2 = torch.zeros((B, 4, 4)).to(tangent)
    rot2[:, 0, 0] = c
    rot2[:, 0, 1] = -s
    rot2[:, 1, 0] = s
    rot2[:, 1, 1] = c
    rot2[:, 2, 2] = 1
    rot2[:, 3, 3] = 1
    return rot2 @ matrix


def get_hips_transform(hips, rightupleg, leftupleg):
    normal = get_hips_normal(hips, rightupleg, leftupleg)
    return get_plane_transform(hips, normal, torch.stack((rightupleg, leftupleg), dim=-2))


def install() -> None:
    """Enregistre ce module comme `util.dataset_mixamo` (et un `util.blender_utils` vide)."""
    me = sys.modules[__name__]
    sys.modules["util.dataset_mixamo"] = me
    stub = types.ModuleType("util.blender_utils")
    stub.bpy = None
    sys.modules["util.blender_utils"] = stub
