import torch
import json

from transformers import AutoTokenizer, AutoModelForSequenceClassification

label_map = {
    "entailment": 0,
    "contradiction": 2,
    "neutral": 1
}


def to_jsonl(data):
    return json.dumps(data).replace("\n", "")


def write_file(data, path, mode="w", **kwargs):
    with open(path, mode=mode, **kwargs) as f:
        f.write(data)


def read_jsonl(path, mode="r", **kwargs):
    # Manually open because .splitlines is different from iterating over lines
    ls = []
    with open(path, mode, **kwargs) as f:
        for line in f:
            ls.append(json.loads(line))
    return ls


def write_jsonl(data, path, mode="w"):
    assert isinstance(data, list)
    lines = [to_jsonl(elem) for elem in data]
    write_file("\n".join(lines) + "\n", path, mode=mode)


class NLICounterfactualFilter:

    def __init__(self, cache) -> None:
        self.hf_model_names = [
            # "textattack/distilbert-base-cased-snli",
            # "MoritzLaurer/DeBERTa-v3-base-mnli",
            "ynie/roberta-large-snli_mnli_fever_anli_R1_R2_R3-nli",
            #"ynie/xlnet-large-cased-snli_mnli_fever_anli_R1_R2_R3-nli"
        ]

        self.tokenizers = {}
        self.models = {}
        self.cache = cache

        for name in self.hf_model_names:
            tokenizer = AutoTokenizer.from_pretrained(name)
            model = AutoModelForSequenceClassification.from_pretrained(name)
            model.cuda()
            model.eval()
            self.tokenizers[name] = tokenizer
            self.models[name] = model

    def predict(self, batch, batch_counter, model_name):
        tokenizer = self.tokenizers[model_name]
        model = self.models[model_name]

        input_seq_pair = tokenizer.batch_encode_plus(
            batch,
            return_tensors="pt",
            truncation=True,
            padding=True,
            return_token_type_ids=False
        )
        input_seq_pair = input_seq_pair.to("cuda")

        counter_seq_pair = tokenizer.batch_encode_plus(
            batch_counter,
            return_tensors="pt",
            truncation=True,
            padding=True,
            return_token_type_ids=False
        )
        counter_seq_pair = counter_seq_pair.to("cuda")

        with torch.no_grad():
            logits = model(**input_seq_pair).logits
            logits_counter = model(**counter_seq_pair).logits

            preds = torch.softmax(logits, -1)
            counter_preds = torch.softmax(logits_counter, -1)

            return preds, counter_preds

    def encode_label(self, label, to_label):
        prev_label_pt = torch.Tensor(label).long().unsqueeze(-1)
        new_label_pt = torch.Tensor(to_label).long().unsqueeze(-1)
        prev_label_ids = torch.zeros(
            prev_label_pt.size()[0], 3).scatter_(-1, prev_label_pt, 1)
        new_label_ids = torch.zeros(
            new_label_pt.size()[0], 3).scatter_(-1, new_label_pt, 1)
        return prev_label_ids.to("cuda"), new_label_ids.to("cuda")

    def critic_metric(self, preds, preds_counter, prev_label_ids, new_label_ids):
        natural = torch.sum(torch.mul(preds, prev_label_ids) -
                            torch.mul(preds_counter, prev_label_ids), -1)
        counter = torch.sum(torch.mul(preds_counter, new_label_ids) -
                            torch.mul(preds, new_label_ids), -1)
        return natural, counter

    def ensemble(self, scores, mode="softmax"):
        if(mode == "softmax"):
            scores_prob = torch.softmax(scores, -1)
            totals = torch.mul(scores_prob, scores)
            return torch.sum(totals, -1)

    def preprocess_batch(self, counter_data):
        batch = list(zip(counter_data["premise"], counter_data["hypothesis"]))
        perturbations = list(
            zip(counter_data["premise"],
                counter_data["span_prev"],
                counter_data["gen_out"])
        )
        p_counter = [p.replace(s, gen) for p, s, gen in perturbations]
        batch_counter = list(zip(p_counter, counter_data["hypothesis"]))
        label = [label_map[x] for x in counter_data["label"]]
        to_label = [label_map[x] for x in counter_data["new_label"]]

        return batch, batch_counter, label, to_label

    def post_process_batch(self, counter_data, batch_counter):
        filtered_data = []
        for i in range(len(batch_counter)):
            guid = counter_data["guid"][i]
            record = json.loads(self.cache.get(guid))
            record["new_premise"] = batch_counter[i][0]
            record["accept"] = counter_data["accept"][i].item()
            self.cache.set(guid, json.dumps(record))
            filtered_data.append(record)
        return filtered_data

    def filter(self, counter_data, threshold=0.5):
        batch, batch_counter, label, to_label = self.preprocess_batch(
            counter_data)

        scores1 = []
        scores2 = []
        for model_name in self.hf_model_names:
            preds, counter_preds = self.predict(
                batch, batch_counter, model_name)
            prev_label_ids, new_label_ids = self.encode_label(
                label, to_label)
            score = self.critic_metric(preds, counter_preds,
                                       prev_label_ids, new_label_ids)
            scores1.append(score[0])
            scores2.append(score[1])

        voting1 = self.ensemble(torch.stack(scores1, -1)).to("cpu").numpy().tolist()
        voting2 = self.ensemble(torch.stack(scores2, -1)).to("cpu").numpy().tolist()
        for i, s in enumerate(zip(voting1, voting2)):
            #print(counter_data['gen_out'][i])
            #print(s[1])
            if s[1] > threshold and s[1] > s[0]:
                counter_data["accept"][i] = True

        return self.post_process_batch(counter_data, batch_counter)