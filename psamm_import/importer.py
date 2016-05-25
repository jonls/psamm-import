# This file is part of PSAMM.
#
# PSAMM is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PSAMM is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PSAMM.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright 2015  Jon Lund Steffensen <jon_steffensen@uri.edu>

"""Generic native model importer"""

from __future__ import print_function

import sys
import os
import argparse
import logging
from collections import OrderedDict, Counter

import yaml
import pkg_resources
from six import iteritems, text_type

from psamm.reaction import Reaction, Direction
from psamm.expression import boolean
from psamm.formula import Formula

from .util import mkdir_p
from .model import ParseError, ModelLoadError


logger = logging.getLogger(__name__)


# Define custom dict representers for YAML
# This allows reading/writing Python OrderedDicts in the correct order.
# See: https://stackoverflow.com/questions/5121931/in-python-how-can-you-load-yaml-mappings-as-ordereddicts  # noqa
def dict_representer(dumper, data):
    return dumper.represent_dict(iteritems(data))


def dict_constructor(loader, node):
    return OrderedDict(loader.construct_pairs(node))


def set_representer(dumper, data):
    return dumper.represent_list(iter(data))


def boolean_expression_representer(dumper, data):
    return dumper.represent_unicode(text_type(data))


def reaction_representer(dumper, data):
    return dumper.represent_unicode(text_type(data))


def formula_representer(dumper, data):
    return dumper.represent_unicode(text_type(data))


def detect_best_flux_limit(model):
    """Detect the best default flux limit to use for model output

    The default flux limit does not change the model but selecting a good
    value reduced the amount of output produced and reduces clutter in the
    output files.
    """

    flux_limit_count = Counter()

    for reaction_id, reaction in iteritems(model.reactions):
        if 'equation' not in reaction.properties:
            continue

        equation = reaction.properties['equation']
        if 'upper_flux' in reaction.properties and equation.direction.forward:
            upper_flux = reaction.properties['upper_flux']
            flux_limit_count[upper_flux] += 1
        if 'lower_flux' in reaction.properties and equation.direction.reverse:
            lower_flux = reaction.properties['lower_flux']
            flux_limit_count[-lower_flux] += 1

    if len(flux_limit_count) == 0:
        return None

    best_flux_limit, _ = flux_limit_count.most_common(1)[0]
    return best_flux_limit


def model_compounds(model):
    """Yield model compounds as YAML dicts"""

    for compound_id, compound in sorted(iteritems(model.compounds)):
        d = OrderedDict()
        d['id'] = compound_id

        order = {
            key: i for i, key in enumerate(
                ['name', 'formula', 'formula_neutral', 'charge', 'kegg',
                 'cas'])}
        prop_keys = (
            set(compound.properties) - {'boundary', 'compartment'})
        for prop in sorted(prop_keys, key=lambda x: (order.get(x, 1000), x)):
            if compound.properties[prop] is not None:
                d[prop] = compound.properties[prop]

        yield d


def model_reactions(model, exchange=False):
    """Yield model reactions as YAML dicts"""

    for reaction_id, reaction in sorted(iteritems(model.reactions)):
        d = OrderedDict()
        d['id'] = reaction_id

        # Check reaction equation
        equation = reaction.properties.get('equation')
        if equation is not None and len(equation.compounds) == 0:
            logger.warning(
                'Reaction {} was removed since it has no compounds.'.format(
                    reaction_id))
            continue

        # Check whether reaction is exchange
        if not exchange and equation is not None:
            if len(equation.compounds) == 1:
                continue

        order = {
            key: i for i, key in enumerate(
                ['name', 'genes', 'equation', 'subsystem', 'ec'])}
        prop_keys = (set(reaction.properties) -
                     {'lower_flux', 'upper_flux', 'reversible'})
        for prop in sorted(prop_keys, key=lambda x: (order.get(x, 1000), x)):
            if reaction.properties[prop] is not None:
                d[prop] = reaction.properties[prop]

        yield d


def model_medium(model, default_flux_limit):
    """Return medium definition as YAML dict"""

    # Count and use the most common compartment as the default compartment
    compartment_count = Counter()
    for reaction_id, reaction in iteritems(model.reactions):
        equation = reaction.properties.get('equation')
        if equation is None or len(equation.compounds) != 1:
            continue
        compound, _ = equation.compounds[0]
        compartment_count[compound.compartment] += 1

    if len(compartment_count) > 0:
        default_compartment, _ = compartment_count.most_common(1)[0]
    else:
        default_compartment = None

    # Generate list of compounds in medium
    compounds = []
    for reaction_id, reaction in sorted(iteritems(model.reactions)):
        equation = reaction.properties.get('equation')
        if equation is None:
            continue

        if len(equation.compounds) != 1:
            continue

        compound, value = equation.compounds[0]

        # Determine the default flux limits. If the value is already at the
        # default it does not need to be included in the output.
        lower_default, upper_default = None, None
        if default_flux_limit is not None:
            lower_default = -default_flux_limit
            upper_default = default_flux_limit

        # We multiply the flux bounds by value in order to create equivalent
        # exchange reactions with stoichiometric value of one. If the flux
        # bounds are not set but the reaction is unidirectional, the implicit
        # flux bounds must be used.
        lower_flux, upper_flux = None, None
        if 'lower_flux' in reaction.properties:
            lower_flux = reaction.properties['lower_flux'] * abs(value)
        elif equation.direction == Direction.Forward:
            lower_flux = 0.0

        if 'upper_flux' in reaction.properties:
            upper_flux = reaction.properties['upper_flux'] * abs(value)
        elif equation.direction == Direction.Reverse:
            upper_flux = 0.0

        # If the stoichiometric value of the reaction is reversed, the flux
        # limits must be flipped.
        if value > 0:
            lower_flux, upper_flux = (
                -upper_flux if upper_flux is not None else None,
                -lower_flux if lower_flux is not None else None)

        c = OrderedDict([('id', compound.name)])
        if compound.compartment != default_compartment:
            c['compartment'] = compound.compartment
        c['reaction'] = reaction_id

        # Assign flux limits if different than the defaults. Also, add 0 so
        # that -0.0 is converted to plain 0.0 which looks better in the output.
        if lower_flux is not None and lower_flux != lower_default:
            c['lower'] = lower_flux + 0
        if upper_flux is not None and upper_flux != upper_default:
            c['upper'] = upper_flux + 0

        compounds.append(c)

    medium = OrderedDict([('name', 'Default medium')])
    if default_compartment is not None:
        medium['compartment'] = default_compartment
    medium['compounds'] = compounds

    return medium


def model_reaction_limits(model, exchange=False, default_flux_limit=None):
    """Yield model reaction limits as YAML dicts"""

    for reaction_id, reaction in sorted(iteritems(model.reactions)):
        # Check whether reaction is exchange
        equation = reaction.properties.get('equation')
        if equation is None:
            continue

        if not exchange and len(equation.compounds) == 1:
            continue

        # Determine the default flux limits. If the value is already at the
        # default it does not need to be included in the output.
        lower_default, upper_default = None, None
        if default_flux_limit is not None:
            if equation.direction.reverse:
                lower_default = -default_flux_limit
            else:
                lower_default = 0.0

            if equation.direction.forward:
                upper_default = default_flux_limit
            else:
                upper_default = 0.0

        lower_flux, upper_flux = None, None
        if ('lower_flux' in reaction.properties and
                reaction.properties['lower_flux'] != lower_default):
            lower_flux = reaction.properties['lower_flux']
        if ('upper_flux' in reaction.properties and
                reaction.properties['upper_flux'] != upper_default):
            upper_flux = reaction.properties['upper_flux']

        if lower_flux is not None or upper_flux is not None:
            d = OrderedDict([('reaction', reaction_id)])
            if lower_flux is not None:
                d['lower'] = lower_flux
            if upper_flux is not None:
                d['upper'] = upper_flux

            yield d


def write_yaml_model(model, dest='.', convert_medium=True):
    """Write the given MetabolicModel to YAML files in dest folder

    The parameter ``convert_medium`` indicates whether the exchange reactions
    should be converted automatically to a medium file.
    """
    yaml.SafeDumper.add_representer(OrderedDict, dict_representer)
    yaml.SafeDumper.add_representer(set, set_representer)
    yaml.SafeDumper.add_representer(frozenset, set_representer)
    yaml.SafeDumper.add_representer(
        boolean.Expression, boolean_expression_representer)
    yaml.SafeDumper.add_representer(Reaction, reaction_representer)
    yaml.SafeDumper.add_representer(Formula, formula_representer)

    yaml.SafeDumper.ignore_aliases = lambda *args: True

    yaml.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                         dict_constructor)

    yaml_args = {'default_flow_style': False,
                 'encoding': 'utf-8',
                 'allow_unicode': True,
                 'width': 79}

    with open(os.path.join(dest, 'compounds.yaml'), 'w+') as f:
        yaml.safe_dump(list(model_compounds(model)), f, **yaml_args)

    default_flux_limit = detect_best_flux_limit(model)
    if default_flux_limit is not None:
        logger.info('Using default flux limit of {}'.format(
            default_flux_limit))

    if convert_medium:
        logger.info('Converting exchange reactions to medium definition')

    exchange = not convert_medium
    with open(os.path.join(dest, 'reactions.yaml'), 'w+') as f:
        reactions = list(model_reactions(model, exchange=exchange))
        yaml.safe_dump(reactions, f, **yaml_args)

    if convert_medium:
        with open(os.path.join(dest, 'medium.yaml'), 'w+') as f:
            yaml.safe_dump(model_medium(model, default_flux_limit), f,
                           **yaml_args)

    reaction_limits = list(model_reaction_limits(
        model, exchange, default_flux_limit))
    if len(reaction_limits) > 0:
        with open(os.path.join(dest, 'limits.yaml'), 'w+') as f:
            yaml.safe_dump(reaction_limits, f, **yaml_args)

    model_d = OrderedDict([('name', model.name)])
    if model.biomass_reaction is not None:
        model_d['biomass'] = model.biomass_reaction
    if default_flux_limit is not None:
        model_d['default_flux_limit'] = default_flux_limit
    model_d.update([
        ('compounds', [{'include': 'compounds.yaml'}]),
        ('reactions', [{'include': 'reactions.yaml'}])])

    if convert_medium:
        model_d['media'] = [{'include': 'medium.yaml'}]

    if len(reaction_limits) > 0:
        model_d['limits'] = [{'include': 'limits.yaml'}]

    with open(os.path.join(dest, 'model.yaml'), 'w+') as f:
        yaml.safe_dump(model_d, f, **yaml_args)


def main():
    parser = argparse.ArgumentParser(
        description='Import from external model formats')
    parser.add_argument('--source', metavar='path', default='.',
                        help='Source directory or file')
    parser.add_argument('--dest', metavar='path', default='.',
                        help='Destination directory (default is ".")')
    parser.add_argument('--no-medium', action='store_true',
                        help='Disable importing exchange reactions as medium')
    parser.add_argument('format', help='Format to import ("list" to see all)')

    args = parser.parse_args()

    # Set up logging for the command line interface
    if 'PSAMM_DEBUG' in os.environ:
        level = getattr(logging, os.environ['PSAMM_DEBUG'].upper(), None)
        if level is not None:
            logging.basicConfig(level=level)
    else:
        logging.basicConfig(
            level=logging.INFO, format=u'%(levelname)s: %(message)s')

    # Discover all available model importers
    importers = {}
    for importer_entry in pkg_resources.iter_entry_points('psamm.importer'):
        canonical = importer_entry.name.lower()
        if canonical not in importers:
            importers[canonical] = importer_entry
        else:
            logger.warning('Importer {} was found more than once!'.format(
                importer_entry.name))

    # Print list of importers
    if args.format in ('list', 'help'):
        if len(importers) == 0:
            logger.error('No importers found!')
        else:
            importer_classes = []
            for name, entry in iteritems(importers):
                importer_class = entry.load()
                title = getattr(importer_class, 'title', None)
                generic = getattr(importer_class, 'generic', False)
                if title is not None:
                    importer_classes.append(
                        (title, generic, name, importer_class))

            print('Generic importers:')
            for title, _, name, importer_class in sorted(
                    c for c in importer_classes if c[1]):
                print('{:<12}  {}'.format(name, title))

            print()
            print('Model-specific importers:')
            for title, _, name, importer_class in sorted(
                    c for c in importer_classes if not c[1]):
                print('{:<12}  {}'.format(name, title))
        sys.exit(0)

    importer_name = args.format.lower()
    if importer_name not in importers:
        logger.error('Importer {} not found!'.format(importer_name))
        logger.info('Use "list" to see available importers.')
        sys.exit(-1)

    importer_class = importers[importer_name].load()
    importer = importer_class()

    try:
        model = importer.import_model(args.source)
    except ModelLoadError as e:
        logger.error('Failed to load model!', exc_info=True)
        importer.help()
        parser.error(text_type(e))
    except ParseError as e:
        logger.error('Failed to parse model!', exc_info=True)
        logger.error(text_type(e))
        sys.exit(-1)

    model.print_summary()

    # Create destination directory if not exists
    dest = args.dest
    mkdir_p(dest)

    write_yaml_model(model, dest, convert_medium=not args.no_medium)
