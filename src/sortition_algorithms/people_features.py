import csv
import typing
from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy

from attrs import define

from sortition_algorithms import errors
from sortition_algorithms.features import FeatureCollection
from sortition_algorithms.people import People
from sortition_algorithms.settings import Settings
from sortition_algorithms.utils import random_provider


@define(kw_only=True, slots=True)
class MaxRatioResult:
    """Result from finding the category with maximum selection ratio."""

    feature_name: str
    feature_value: str
    random_person_index: int


class PeopleFeatures:
    """
    This class manipulates people and features together, making a deepcopy on init.
    """

    # TODO: consider naming: maybe SelectionState

    def __init__(
        self,
        people: People,
        features: FeatureCollection,
        check_same_address: bool = False,
        check_same_address_columns: list[str] | None = None,
    ) -> None:
        self.people = deepcopy(people)
        self.features = deepcopy(features)
        self.check_same_address = check_same_address
        self.check_same_address_columns = check_same_address_columns or []

    def update_features_remaining(self, person_key: str) -> None:
        # this will blow up if the person does not exist
        person = self.people.get_person_dict(person_key)
        for feature_name in self.features.feature_names:
            self.features.add_remaining(feature_name, person[feature_name])

    def update_all_features_remaining(self) -> None:
        for person_key in self.people:
            self.update_features_remaining(person_key)

    def delete_all_with_feature_value(self, feature_name: str, feature_value: str) -> tuple[int, int]:
        """
        When a feature/value is "full" we delete everyone else in it.
        "Full" means that the number selected equals the "max" amount - that
        is detected elsewhere and then this method is called.
        Returns count of those deleted, and count of those left
        """
        # when a category is full we want to delete everyone in it
        people_to_delete: list[str] = []
        for pkey, person in self.people.items():
            if person[feature_name] == feature_value:
                people_to_delete.append(pkey)
                for feature in self.features.feature_names:
                    try:
                        self.features.remove_remaining(feature, person[feature])
                    except errors.SelectionError as e:
                        msg = (
                            f"SELECTION IMPOSSIBLE: FAIL in delete_all_in_feature_value() "
                            f"as after previous deletion no one/not enough left in {feature} "
                            f"{person[feature]}. Tried to delete: {len(people_to_delete)}"
                        )
                        raise errors.SelectionError(msg) from e

        self.people.remove_many(people_to_delete)
        # return the number of people deleted and the number of people left
        return len(people_to_delete), self.people.count

    def prune_for_feature_max_0(self) -> list[str]:
        """
        Check if any feature_value.max is set to zero. if so delete everyone with that feature value
        NOT DONE: could then check if anyone is left.
        """
        # TODO: when do we want to do this?
        msg: list[str] = []
        msg.append(f"Number of people: {self.people.count}.")
        total_num_deleted = 0
        for (
            feature_name,
            feature_value,
            fv_counts,
        ) in self.features.feature_values_counts():
            if fv_counts.max == 0:  # we don't want any of these people
                # pass the message in as deleting them might throw an exception
                msg.append(f"Feature/value {feature_name}/{feature_value} full - deleting people...")
                num_deleted, num_left = self.delete_all_with_feature_value(feature_name, feature_value)
                # if no exception was thrown above add this bit to the end of the previous message
                msg[-1] += f" Deleted {num_deleted}, {num_left} left."
                total_num_deleted += num_deleted
        # if the total number of people deleted is lots then we're probably doing a replacement selection, which means
        # the 'remaining' file will be useless - remind the user of this!
        if total_num_deleted >= self.people.count / 2:
            msg.append(
                ">>> WARNING <<< That deleted MANY PEOPLE - are you doing a "
                "replacement? If so your REMAINING FILE WILL BE USELESS!!!"
            )
        return msg

    def select_person(self, person_key: str) -> list[str]:
        """
        Selecting a person means:
        - remove the person from our copy of People
        - update the `selected` and `remaining` counts of the FeatureCollection
        - if check_same_address is True, also remove household members (without adding to selected)

        Returns:
            List of additional people removed due to same address (empty if check_same_address is False)
        """
        # First, find household members if address checking is enabled (before removing the person)
        household_members_removed = []
        if self.check_same_address and self.check_same_address_columns:
            household_members_removed = list(self.people.matching_address(person_key, self.check_same_address_columns))

        # Handle the main person selection
        person = self.people.get_person_dict(person_key)
        for feature in self.features.feature_names:
            self.features.remove_remaining(feature, person[feature])
            self.features.add_selected(feature, person[feature])
        self.people.remove(person_key)

        # Then remove household members if any were found
        for household_member_key in household_members_removed:
            household_member = self.people.get_person_dict(household_member_key)
            for feature in self.features.feature_names:
                self.features.remove_remaining(feature, household_member[feature])
                # Note: we don't call add_selected() for household members
            self.people.remove(household_member_key)

        return household_members_removed

    def find_max_ratio_category(self) -> MaxRatioResult:
        """
        Find the feature/value combination with the highest selection ratio.

        The ratio is calculated as: (min - selected) / remaining
        This represents how urgently we need people from this category.
        Higher ratio = more urgent need (fewer people available relative to what we still need).

        Returns:
            MaxRatioResult containing the feature name, value, and a random person index

        Raises:
            SelectionError: If insufficient people remain to meet minimum requirements
        """
        max_ratio = -100.0
        result_feature_name = ""
        result_feature_value = ""
        random_person_index = -1

        for (
            feature_name,
            feature_value,
            fv_counts,
        ) in self.features.feature_values_counts():
            # Check if we have insufficient people to meet minimum requirements
            people_still_needed = fv_counts.min - fv_counts.selected
            if fv_counts.selected < fv_counts.min and fv_counts.remaining < people_still_needed:
                msg = (
                    f"SELECTION IMPOSSIBLE: Not enough people remaining in {feature_name}/{feature_value}. "
                    f"Need {people_still_needed} more, but only {fv_counts.remaining} remaining."
                )
                raise errors.SelectionError(msg)

            # Skip categories with no remaining people or max = 0
            if fv_counts.remaining == 0 or fv_counts.max == 0:
                continue

            # Calculate the priority ratio
            ratio = people_still_needed / float(fv_counts.remaining)

            # Track the highest ratio category
            if ratio > max_ratio:
                max_ratio = ratio
                result_feature_name = feature_name
                result_feature_value = feature_value
                # from 1 to remaining
                random_person_index = random_provider().randbelow(fv_counts.remaining) + 1

        # If no valid category found, all categories must be at their max or have max=0
        if not result_feature_name:
            msg = "No valid categories found - all may be at maximum or have max=0"
            raise errors.SelectionError(msg)

        return MaxRatioResult(
            feature_name=result_feature_name,
            feature_value=result_feature_value,
            random_person_index=random_person_index,
        )

    def handle_category_full_deletions(self, selected_person_data: dict[str, str]) -> list[str]:
        """
        Check if any categories are now full after a selection and delete remaining people.

        When a person is selected, some categories may reach their maximum quota.
        This method identifies such categories and removes all remaining people from them.

        Args:
            selected_person_data: Dictionary of the selected person's feature values

        Returns:
            List of output messages about categories that became full and people deleted

        Raises:
            SelectionError: If deletions would violate minimum constraints
        """
        output_messages = []

        for (
            feature_name,
            feature_value,
            fv_counts,
        ) in self.features.feature_values_counts():
            if feature_value == selected_person_data[feature_name] and fv_counts.selected == fv_counts.max:
                num_deleted, num_left = self.delete_all_with_feature_value(feature_name, feature_value)
                if num_deleted > 0:
                    output_messages.append(
                        f"Category {feature_name}/{feature_value} full - deleted {num_deleted} people, {num_left} left."
                    )

        return output_messages


def simple_add_selected(person_keys: Iterable[str], people: People, features: FeatureCollection) -> None:
    """
    Just add the person to the selected counts for the feature values for that person.
    Don't do the more complex handling of the full PeopleFeatures.add_selected()
    """
    for person_key in person_keys:
        person = people.get_person_dict(person_key)
        for feature_name in features.feature_names:
            features.add_selected(feature_name, person[feature_name])


class WeightedSample:
    def __init__(self, features: FeatureCollection) -> None:
        """
        This produces a set of lists of feature values for each feature.  Each value
        is in the list `fv_counts.max` times - so a random choice with represent the max.

        So if we had feature "ethnicity", value "white" w max 4, "asian" w max 3 and
        "black" with max 2 we'd get:

        ["white", "white", "white", "white", "asian", "asian", "asian", "black", "black"]

        Then making random choices from that list produces a weighted sample.
        """
        self.weighted: dict[str, list[str]] = defaultdict(list)
        for feature_name, value, fv_counts in features.feature_values_counts():
            self.weighted[feature_name] += [value] * fv_counts.max

    def value_for(self, feature_name: str) -> str:
        # S311 is random numbers for crypto - but this is just for a sample file
        return random_provider().choice(self.weighted[feature_name])


def create_readable_sample_file(
    features: FeatureCollection,
    people_file: typing.TextIO,
    number_people_example_file: int,
    settings: Settings,
) -> None:
    example_people_writer = csv.writer(
        people_file,
        delimiter=",",
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
    )
    example_people_writer.writerow([settings.id_column, *settings.columns_to_keep, *features.feature_names])
    weighted = WeightedSample(features)
    for x in range(number_people_example_file):
        row = [
            f"p{x}",
            *[f"{col} {x}" for col in settings.columns_to_keep],
            *[weighted.value_for(f) for f in features.feature_names],
        ]
        example_people_writer.writerow(row)
