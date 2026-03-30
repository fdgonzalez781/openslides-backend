from collections import defaultdict
from decimal import Decimal
from typing import Any, cast

from psycopg.types.json import Jsonb

from openslides_backend.shared.typing import HistoryInformation

from ....services.database.commands import GetManyRequest
from ....shared.exceptions import ActionException, VoteServiceException
from ....shared.patterns import collection_from_fqid, fqid_from_collection_and_id
from ...action import Action
from ..option.set_auto_fields import OptionSetAutoFields
from ..projector_countdown.mixins import CountdownCommand, CountdownControl
from ..vote.create import VoteCreate
from ..vote.user_token_helper import get_user_token
from .functions import check_poll_or_option_perms


class PollValidationMixin(Action):
    def validate_instance(self, instance: dict[str, Any]) -> None:
        super().validate_instance(instance)

        if poll_id := instance.get("id"):
            poll = self.datastore.get(
                fqid_from_collection_and_id("poll", poll_id),
                ["max_votes_amount", "min_votes_amount", "max_votes_per_option"],
            )
        max_votes_amount = cast(
            int,
            instance.get(
                "max_votes_amount", poll["max_votes_amount"] if poll_id else 1
            ),
        )
        min_votes_amount = cast(
            int,
            instance.get(
                "min_votes_amount", poll["min_votes_amount"] if poll_id else 1
            ),
        )
        max_votes_per_option = cast(
            int,
            instance.get(
                "max_votes_per_option", poll["max_votes_per_option"] if poll_id else 1
            ),
        )

        if max_votes_amount < max_votes_per_option:
            raise ActionException(
                "The maximum votes per option cannot be higher than the maximum amount of votes in total."
            )
        if max_votes_amount < min_votes_amount:
            raise ActionException(
                "The minimum amount of votes cannot be higher than the maximum amount of votes."
            )


class PollPermissionMixin(Action):
    def check_permissions(self, instance: dict[str, Any]) -> None:
        if "meeting_id" in instance:
            content_object_id = instance.get("content_object_id", "")
            meeting_id = instance["meeting_id"]
        else:
            poll = self.datastore.get(
                fqid_from_collection_and_id("poll", instance["id"]),
                ["content_object_id", "meeting_id"],
                lock_result=False,
            )
            content_object_id = poll.get("content_object_id", "")
            meeting_id = poll["meeting_id"]
        if not content_object_id:
            raise ActionException("No 'content_object_id' was given")
        check_poll_or_option_perms(
            content_object_id, self.datastore, self.user_id, meeting_id
        )


class StopControl(CountdownControl, Action):
    def on_stop(self, instance: dict[str, Any]) -> None:
        poll = self.datastore.get(
            fqid_from_collection_and_id(self.model.collection, instance["id"]),
            [
                "state",
                "meeting_id",
                "pollmethod",
                "global_option_id",
                "entitled_group_ids",
                "content_object_id",
                "option_ids",
            ],
        )
        # reset countdown given by meeting
        meeting = self.datastore.get(
            fqid_from_collection_and_id("meeting", poll["meeting_id"]),
            [
                "poll_couple_countdown",
                "poll_countdown_id",
                "users_enable_vote_weight",
                "users_enable_vote_delegations",
            ],
        )
        if meeting.get("poll_couple_countdown") and meeting.get("poll_countdown_id"):
            self.control_countdown(meeting["poll_countdown_id"], CountdownCommand.RESET)

        self.logger.debug(
            f"here's the instance right before it all goes wrong: {instance}"
        )
        # stop poll in vote service and create vote objects
        results = self.vote_service.stop(instance["id"])
        self.logger.debug(f"and here are the results: {results}")
        if poll["pollmethod"] == "STV":
            self.logger.debug(f"content object id: {poll['content_object_id']}")
            assignment = self.datastore.get(
                poll["content_object_id"],
                ["open_posts"],
            )
            stv_results = self.handle_stv_election(
                poll["option_ids"],
                assignment["open_posts"],
                results,
                instance,
                poll,
                meeting,
            )
            self.logger.debug(f"here are the results from STV! {stv_results}")
            return
        action_data = []
        votesvalid = Decimal("0.000000")
        option_results: dict[int, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: Decimal("0.000000"))
        )  # maps options to their respective YNA sums
        for ballot in results["votes"]:
            user_token = get_user_token()
            vote_weight = Decimal(ballot["weight"])
            votesvalid += vote_weight
            vote_template: dict[str, str | int] = {"user_token": user_token}
            if "vote_user_id" in ballot:
                vote_template["user_id"] = ballot["vote_user_id"]
            if "request_user_id" in ballot:
                vote_template["delegated_user_id"] = ballot["request_user_id"]

            if isinstance(ballot["value"], dict):
                for option_id_str, value in ballot["value"].items():
                    option_id = int(option_id_str)

                    self.logger.debug(f"Vote value: {value}")
                    vote_value = value
                    vote_weighted = vote_weight  # use new variable vote_weighted because pollmethod=Y/N does not imply anymore that only one loop is done (see max_votes_per_option)
                    if poll["pollmethod"] in ("Y", "N"):
                        if value == 0:
                            continue
                        vote_value = poll["pollmethod"]
                        vote_weighted *= value

                    option_results[option_id][vote_value] += vote_weighted
                    action_data.append(
                        {
                            "value": vote_value,
                            "option_id": option_id,
                            "weight": str(vote_weighted),
                            **vote_template,
                        }
                    )
            elif isinstance(ballot["value"], str):
                vote_value = ballot["value"]
                option_id = poll["global_option_id"]
                option_results[option_id][vote_value] += vote_weight
                action_data.append(
                    {
                        "value": vote_value,
                        "option_id": option_id,
                        "weight": str(vote_weight),
                        **vote_template,
                    }
                )
            else:
                raise VoteServiceException("Invalid response from vote service")
        self.execute_other_action(VoteCreate, action_data)
        # update results into option
        self.execute_other_action(
            OptionSetAutoFields,
            [
                {
                    "id": _id,
                    "yes": str(option["Y"]),
                    "no": str(option["N"]),
                    "abstain": str(option["A"]),
                }
                for _id, option in option_results.items()
            ],
        )
        # set voted ids
        voted_ids = results["user_ids"]
        instance["voted_ids"] = voted_ids

        # set votescast, votesvalid, votesinvalid
        instance["votesvalid"] = str(votesvalid)
        instance["votescast"] = str(Decimal("0.000000") + Decimal(len(voted_ids)))
        instance["votesinvalid"] = "0.000000"

        # set entitled users at stop.
        instance["entitled_users_at_stop"] = Jsonb(
            self.get_entitled_users(poll | instance, meeting)
        )

    def handle_stv_election(
        self,
        hopeful: list[int],
        open_seats: int,
        results: dict[str, Any],
        instance: dict[str, Any],
        poll: dict[str, Any],
        meeting: dict[str, Any],
    ):
        ballots = results["votes"]
        votesvalid = Decimal("0.000000")
        numseats = Decimal("0.000000") + open_seats
        action_data = []
        for b in ballots:
            votesvalid += Decimal(b["weight"])
            user_token = get_user_token()
            vote_template: dict[str, str | int] = {"user_token": user_token}
            if "vote_user_id" in b:
                vote_template["user_id"] = b["vote_user_id"]
            if "request_user_id" in b:
                vote_template["delegated_user_id"] = b["request_user_id"]
            for rank, c in enumerate(b["value"], start=1):
                action_data.append(
                    {
                        "value": "Y",
                        "option_id": c,
                        "weight": f"{Decimal(1.000000):.6f}",
                        "rank": rank,
                        **vote_template,
                    }
                )

        quota = (votesvalid / (numseats + 1)) + 1

        weighted_ballots = map(
            lambda b: {
                "data": b,
                "ranking": b["value"],
                "weight": Decimal(b["weight"]),
                "transfer_value": Decimal("1.000000"),
            },
            ballots,
        )
        elected = []
        eliminated = []
        id_exhausted = -1
        candidate_vote_totals: dict[int, Decimal] = {
            c: Decimal("0.000000") for c in hopeful
        }
        vote_buckets: dict[int, list[dict[str, Any]]] = defaultdict(lambda: [])
        round_by_round: dict[int, dict[int, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: Decimal("0.000000"))
        )
        # round_by_round: list[str] = []
        round = 0

        # Compute first preference totals
        for ballot in weighted_ballots:
            ranking = ballot["ranking"]
            weight = ballot["weight"]

            first_pref = ranking[0]
            candidate_vote_totals[first_pref] += weight
            round_by_round[round][first_pref] += weight
            # round_by_round.append(f"{round}/{first_pref}/{weight:.6f}")
            vote_buckets[first_pref].append(ballot)

        self.logger.debug(
            f"There are {votesvalid} votes cast and {numseats} open seats. The quota is {quota}."
        )
        self.logger.debug(
            f"The following candidates are standing for election: {hopeful}"
        )

        while numseats - len(elected) > 0:
            # Determine elected candidates, transfer surplus
            round += 1
            self.logger.debug(f"***** ROUND {round} *****")
            candidates_sorted = sorted(
                filter(lambda item: item[0] in hopeful, candidate_vote_totals.items()),
                key=lambda item: item[1],
                reverse=True,
            )
            self.logger.debug(f"Current standings: {candidates_sorted}")
            self.logger.debug(
                f"The election is for a total of {numseats} seats, and the following {len(elected)} candidates have been elected: {elected}. There are {numseats - len(elected)} open seats remaining."
            )
            candidate = 0
            candidate_votes = 0

            # TODO: Resolve ties
            hopeful_winner = candidates_sorted[0]
            self.logger.debug(
                f"Candidate {hopeful_winner[0]} is in first place. Checking if candidate is past quota."
            )
            if hopeful_winner[1] >= quota:
                candidate = hopeful_winner[0]
                candidate_votes = hopeful_winner[1]
                self.logger.debug(
                    f"Candidate {hopeful_winner[0]} meets quota with {hopeful_winner[1]} votes. Proceeding with election."
                )
            # DEBUG ONLY
            else:
                self.logger.debug(
                    f"With {hopeful_winner[1]} votes, candidate {hopeful_winner[0]} does not meet quota. Proceeding with elimination of last place candidate."
                )

            # DEBUG ONLY
            if candidate:
                self.logger.debug(f"Candidate being processed is {candidate}.")
            else:
                self.logger.debug(
                    "No winner found. Proceeding with elimination of last place candidate."
                )

            # Candidate is elected
            if candidate:
                self.logger.debug(
                    f"Candidate {candidate} is elected with {candidate_votes} votes."
                )
                elected.append(candidate)
                hopeful.remove(candidate)
                transfer_value = (candidate_votes - quota) / candidate_votes
                for ballot in vote_buckets[candidate]:
                    ballot["transfer_value"] *= transfer_value
            # Nobody is elected; candidate is eliminated
            else:
                # But there aren't enough candidates left to eliminate! Everyone remaining in the election is elected.
                if len(candidates_sorted) == (numseats - len(elected)):
                    self.logger.debug(
                        f"Election is exhausted. All remaining hopeful candidates {hopeful} are elected."
                    )
                    for c in hopeful:
                        elected.append(c)
                    hopeful.clear()
                else:
                    # TODO: Resolve ties
                    # count = 1
                    candidate = candidates_sorted[len(candidates_sorted) - 1][0]
                    candidate_votes = candidates_sorted[len(candidates_sorted) - 1][1]
                    self.logger.debug(
                        f"Candidate {candidate} does not meet quota with only {candidate_votes} votes and is eliminated."
                    )
                    eliminated.append(candidate)
                    hopeful.remove(candidate)

            while candidate != 0 and len(vote_buckets[candidate]) > 0:
                ballot = vote_buckets[candidate].pop()
                ranking = ballot["ranking"]
                self.logger.debug(
                    f"Transferring ballot {ranking}. Searching for next available preference."
                )

                n = 1
                while n < len(ranking) and ranking[n] not in hopeful:
                    self.logger.debug(
                        f"Candidate {ranking[n]} is not in the running. Skipping to next preference."
                    )
                    n += 1

                if n < len(ranking):
                    orig_pref = ranking[0]
                    next_pref = ranking[n]
                    self.logger.debug(
                        f"Next available preference found: candidate {next_pref}."
                    )
                    final_weight = ballot["weight"] * ballot["transfer_value"]

                    ballot["ranking"] = ballot["ranking"][1:]
                    candidate_vote_totals[next_pref] += final_weight
                    round_by_round[round][next_pref] += final_weight
                    # round_by_round.append(f"{round}/{next_pref}/{final_weight:.6f}")
                    self.logger.debug(
                        f"Vote is transferred from candidate {orig_pref} to candidate {next_pref} at value {ballot['transfer_value']}."
                    )
                    vote_buckets[next_pref].append(ballot)

                else:
                    self.logger.debug("No next preference found. Ballot is exhausted.")
                    vote_buckets[id_exhausted].append(ballot)

        self.logger.debug(
            f"Election is completed. Candidates {elected} have been elected."
        )
        self.logger.debug(f"Final vote totals: {candidate_vote_totals}")

        self.execute_other_action(VoteCreate, action_data)
        # update results into option
        self.execute_other_action(
            OptionSetAutoFields,
            [
                {
                    "id": _id,
                    "yes": f"{votes:.6f}",
                    "no": str(Decimal("0.000000")),
                    "abstain": str(Decimal("0.000000")),
                }
                for _id, votes in candidate_vote_totals.items()
            ],
        )
        # set voted ids
        voted_ids = results["user_ids"]
        instance["voted_ids"] = voted_ids

        # set votescast, votesvalid, votesinvalid
        instance["votesvalid"] = str(votesvalid)
        instance["votescast"] = str(Decimal("0.000000") + Decimal(len(voted_ids)))
        instance["votesinvalid"] = "0.000000"

        # set quota, round_by_round
        instance["quota"] = str(f"{quota:.6f}")

        # process round_by_round to list[str]
        round_by_round_str: list[str] = []
        for single_round_results in round_by_round.items():
            rnd = single_round_results[0]
            # single_round_results is (int, dict[int, Decimal]) where each dict[int, Decimal] maps a candidate id to their results for that round
            for result_per_candidate in single_round_results[1].items():
                cand = result_per_candidate[0]
                vts = result_per_candidate[1]
                round_by_round_str.append(f"{rnd}/{cand}/{vts:.6f}")
        instance["round_by_round"] = round_by_round_str
        self.logger.debug(f"Round by round results: {round_by_round}")

        # set entitled users at stop.
        instance["entitled_users_at_stop"] = Jsonb(
            self.get_entitled_users(poll | instance, meeting)
        )

    def get_entitled_users(
        self, poll: dict[str, Any], meeting: dict[str, Any]
    ) -> list[dict[str, Any]]:
        entitled_users = []
        all_voted_users = set(poll.get("voted_ids", []))

        # get all users from the groups.
        gmr = GetManyRequest(
            "group", poll.get("entitled_group_ids", []), ["meeting_user_ids"]
        )
        gm_result = self.datastore.get_many([gmr])
        groups = gm_result.get("group", {}).values()

        # fetch presence status
        meeting_user_ids = set()
        for group in groups:
            meeting_user_ids.update(group.get("meeting_user_ids", []))
        gmr = GetManyRequest(
            "meeting_user", list(meeting_user_ids), ["user_id", "vote_delegated_to_id"]
        )
        gm_result = self.datastore.get_many([gmr])
        meeting_users = gm_result.get("meeting_user", {}).values()

        mu_to_user_id = {}
        if meeting.get("users_enable_vote_delegations"):
            # fetch vote delegations
            delegated_to_mu_ids = list(
                {id_ for mu in meeting_users if (id_ := mu.get("vote_delegated_to_id"))}
            )
            if delegated_to_mu_ids:
                gmr = GetManyRequest("meeting_user", delegated_to_mu_ids, ["user_id"])
                mu_to_user_id = self.datastore.get_many([gmr]).get("meeting_user", {})

        gmr = GetManyRequest(
            "user",
            [mu["user_id"] for mu in meeting_users],
            ["is_present_in_meeting_ids"],
        )
        users = self.datastore.get_many([gmr]).get("user", {})

        for mu in meeting_users:
            entitled_users.append(
                {
                    "voted": mu["user_id"] in all_voted_users,
                    "present": poll["meeting_id"]
                    in users[mu["user_id"]].get("is_present_in_meeting_ids", []),
                    "user_id": mu["user_id"],
                    "vote_delegated_to_user_id": (
                        mu_to_user_id[vote_mu_id]["user_id"]
                        if (vote_mu_id := mu.get("vote_delegated_to_id"))
                        and meeting.get("users_enable_vote_delegations")
                        else None
                    ),
                }
            )

        return entitled_users


class PollHistoryMixin(Action):
    poll_history_information: str

    def get_history_information(self) -> HistoryInformation | None:
        # no datastore access necessary if information is in payload
        polls = self.get_instances_with_fields(["content_object_id"])
        return {
            poll["content_object_id"]: [
                f"{self.get_history_title(poll)} {self.poll_history_information}"
            ]
            for poll in polls
        }

    def get_history_title(self, poll: dict[str, Any]) -> str:
        content_collection = collection_from_fqid(poll["content_object_id"])
        if content_collection == "assignment":
            return "Ballot"
        return "Voting"
