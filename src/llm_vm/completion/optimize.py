import openai
import traceback
import threading
import time
import os
import sys
import signal 
import json
import tempfile
import abc
import requests
import hashlib
import pickle

#we need to package-ify so this works 
import llm_vm.completion.data_synthesis as data_synthesis


job_id = None # we want to be able to cancel a fine_tune if you kill the program

def exit_handler(signum, frame):
    
    if (None != job_id):
        print("cancelling fine-tune if applicable")
        openai.FineTune.cancel(id=job_id)

    print("user interrupt, exiting")
    sys.exit()

signal.signal(signal.SIGINT, exit_handler)


def generate_hash(input_string):
    sha256_hash = hashlib.sha256()
    sha256_hash.update(str(input_string).encode('utf-8'))
    return int(sha256_hash.hexdigest(), 16) % 10**18

def asyncStart(foo):
    t = [None, None]
    def new_thread():
        t[0] = foo()
    t[1] = threading.Thread(target=new_thread)
    t[1].start()
    return t

def asyncAwait(t):
    t[1].join()
    return t[0]


class local_ephemeral:

    def __init__(self):
        self.training_store = {}

    def get_data(self, c_id):
        self.init_if_null(c_id)
        return self.training_store[c_id]["data"]
    
    def load_data(self,file):
        #using pickle files right now, in the future we will have to use databases
        self.training_store = pickle.load(file)

    def store_data(self,file):
         #using pickle files right now, in the future we will have to use databases
        pickle.dump(self.training_store,file)

    def add_example(self, c_id, example):
        self.init_if_null(c_id)
        self.training_store[c_id]["data"] += [example]

    def set_training_in_progress(self, c_id, is_training):
        self.init_if_null(c_id)
        self.training_store[c_id]["is_training"] = is_training

    def get_training_in_progress_set_true(self, c_id):
        self.init_if_null(c_id)
        # TODO: this could cause a concurrency bug when distributed!
        self.training_store[c_id]['lock'].acquire()
        old_val = self.training_store[c_id]["is_training"]
        if not old_val:
            self.training_store[c_id]["is_training"] = True
        self.training_store[c_id]['lock'].release()
        return old_val

    def set_model(self, c_id, model_id):
        self.init_if_null(c_id)
        self.training_store[c_id]["model"] = model_id

    def get_model(self, c_id):
        self.init_if_null(c_id)
        return self.training_store[c_id]["model"]

    def init_if_null(self, c_id):
        if not c_id in self.training_store:
            self.training_store[c_id] = { "is_training": False,
                                          'lock' : threading.Lock(),
                                          "data": [],
                                          "model": None }


def CALL_BIG(prompt, gpt4=False, **kwargs):
    print("HERE",os.getenv("OPENAI_API_KEY"))
    cur_prompt = [{'role': "system", 'content' : prompt}]
    print("CUR_PROMPT:", cur_prompt, flush=True)
    print("KWARGS:", kwargs, flush=True)
    ans = openai.ChatCompletion.create(
        messages=cur_prompt,
        model="gpt-3.5-turbo-0301" if not gpt4 else 'gpt-4',
        **kwargs)

    return ans['choices'][0]['message']['content']

def CALL_SMALL(*args, **kwargs):
    ans = openai.Completion.create(*args, **kwargs)
    return ans['choices'][0]['text']



class Optimizer:
    @abc.abstractmethod
    def complete(self, stable_context, dynamic_prompt, **kwargs):
        """
        Runs a completion using the string stable_context+dynamic_prompt.  Returns an optional training closure to use if the 
        caller decides that the completion was particularly good.
        
        This method first checks if a model exists for the stable_context. If it does, it uses the model to complete the prompt. 
        It then checks if the number of training examples is less than the maximum allowable. If it is, or if a model wasn't 
        previously found, it retrieves the best completion for the prompt using a larger model, adds a new datapoint for training, 
        and potentially fine-tunes a new model using the updated data, storing the new model if successful. 

        The function returns the best completion (either generated by the stored model or the larger model).

        This can not handle cases where either stable_context or dynamic_prompt are just whitespace!

        Parameters:
        - stable_context (str): Stable contextual data to use as a basis for training.
        - dynamic_prompt (str): The dynamic data to generate a completion for and potentially add to training data.
        - **kwargs: Additional arguments to be passed to the `call_small` and `call_big` methods.

        Returns:
        - completion (str): The best completion for the dynamic prompt, as generated by either the stored model or the larger model.
        """


class HostedOptimizer(Optimizer):
    def __init__(self, anarchy_key, openai_key, MIN_TRAIN_EXS=20, MAX_TRAIN_EXS = 2000):
        self.anarchy_key = anarchy_key
        self.openai_key = openai_key
        self.MIN_TRAIN_EXS = MIN_TRAIN_EXS
        self.MAX_TRAIN_EXS = MAX_TRAIN_EXS

    def complete(self, stable_context, dynamic_prompt, **kwargs):
        """
        TODO: Runs the optimizing completion process on anarchy's hosted server with persistence.

        Parameters:
        - stable_context (str): Stable contextual data to use as a basis for training.
        - dynamic_prompt (str): The dynamic data to generate a completion for and potentially add to training data.
        - **kwargs: Additional arguments to be passed to the `call_small` and `call_big` methods.

        Returns:
        - completion (str): The best completion for the dynamic prompt, as generated by either the stored model or the larger model.
        """
        url = "https://api.chat.dev/completion/optimizing"
        payload = {**kwargs, 
                   'stable_context': stable_context, 
                   'dynamic_prompt': dynamic_prompt,
                   'anarchy_key' : self.anarchy_key,
                   'openai_key' : self.openai_key,
                   'MIN_TRAIN_EXS' : self.MIN_TRAIN_EXS,
                   'MAX_TRAIN_EXS' : self.MAX_TRAIN_EXS
                   }
        headers = {'Authorization': f'Bearer {self.anarchy_key}'}

        print("Payload: ", payload)
        try:
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()  # Raise an exception for 4XX and 5XX status codes
            return response.json()['completion']
        except requests.exceptions.RequestException as e:
            print("Error occurred:", e)
        
    
class LocalOptimizer(Optimizer):
    def __init__(self, storage=local_ephemeral(), MIN_TRAIN_EXS = 20, MAX_TRAIN_EXS = 2000, call_small = CALL_SMALL, call_big = CALL_BIG, openai_key=""):
        self.storage = storage
        self.MIN_TRAIN_EXS = MIN_TRAIN_EXS
        self.MAX_TRAIN_EXS = MAX_TRAIN_EXS
        self.call_small = call_small
        self.call_big = call_big
        self.openai_key = openai_key
        self.data_synthesizer = data_synthesis.DataSynthesis(0,100)

    def complete(self, stable_context, dynamic_prompt, data_synthesis = False, finetune = False, **kwargs):
        openai.api_key = self.openai_key
        completion, train = self.complete_delay_train(stable_context, dynamic_prompt, run_data_synthesis=data_synthesis, **kwargs)
        if finetune:
            train()
        return completion
    
    def complete_delay_train(self, stable_context, dynamic_prompt, run_data_synthesis = False, min_examples_for_synthesis = 5,c_id = None, **kwargs):
        """
        Runs a completion using the string stable_context+dynamic_prompt.  Returns an optional training closure to use if the 
        caller decides that the completion was particularly good.
        
        This method first checks if a model exists for the stable_context. If it does, it uses the model to complete the prompt. 
        It then checks if the number of training examples is less than the maximum allowable. If it is, or if a model wasn't 
        previously found, it retrieves the best completion for the prompt using a larger model, adds a new datapoint for training, 
        and potentially fine-tunes a new model using the updated data, storing the new model if successful. 

        The function returns the best completion (either generated by the stored model or the larger model), and a closure 
        function that encapsulates the fine-tuning process for potential execution at a later time.

        Parameters:
        - stable_context (str): Stable contextual data to use as a basis for training.
        - dynamic_prompt (str): The dynamic data to generate a completion for and potentially add to training data.
        - c_id (str): To be used if multiple users could share the same stable_contexts so that we don't leak data.  If its None, defaults to all possible context.
        - **kwargs: Additional arguments to be passed to the `call_small` and `call_big` methods.

        Returns:
        - completion (str): The best completion for the dynamic prompt, as generated by either the stored model or the larger model.
        - succeed_train (function): A closure function that encapsulates the fine-tuning process, ready for 
        execution at a later time.  If you pass it a completion, it will use that, otherwise it will use the completion from the "best" model.
        """
        assert dynamic_prompt.strip() != "" or stable_context.strip() != ""

        if stable_context.strip() == "" :
            print("Running with an empty context")
        
        prompt = (stable_context + dynamic_prompt).strip()
        c_id_repr = str({'stable_context' : stable_context, 
                    'args' : kwargs, 
                    'MIN_TRAIN_EXS' : self.MIN_TRAIN_EXS, 
                    'MAX_TRAIN_EXS' : self.MAX_TRAIN_EXS,
                    'call_small' : str(self.call_small).split(' ')[1], # HACKS 
                    'call_big' : str(self.call_big).split(' ')[1],
                    }) if c_id is None else c_id
        c_id = generate_hash(c_id_repr)

        completion = None
        model = self.storage.get_model(c_id)
        if model is not None:
            print("Using the new model:", model, flush=True)
            completion = self.call_small(prompt = dynamic_prompt.strip(), model=model, **kwargs)
     
        training_exs = self.storage.get_data(c_id)
        
        best_completion_promise = None
        succeed_train = None
        if completion is None or len(training_exs) < self.MAX_TRAIN_EXS:
            def promiseCompletion():
                best_completion = self.call_big(prompt, **kwargs)
                
                def actual_train(use_completion = None):
                   
                    train_completion = best_completion if use_completion is None else use_completion
                    new_datapoint = (dynamic_prompt.strip(), train_completion)
                    self.storage.add_example(c_id, new_datapoint)
                   
                    if run_data_synthesis:
                        if len(self.storage.get_data(c_id)) < min_examples_for_synthesis:
                            print("Data synthesis is not available right now, need more examples in storage.")
                        else:
                            for j in self.data_synthesizer.data_synthesis(self,prompt,best_completion):
                                self.storage.add_example(c_id, j)
                    training_exs = self.storage.get_data(c_id)
                    
                    print("Considering Fine-tuning", flush=True)
                    
                    if len(training_exs) >= self.MIN_TRAIN_EXS and not self.storage.get_training_in_progress_set_true(c_id):
                        print("Actually Fine-tuning", flush=True)
                        print("Training examples:",str(len(training_exs)))
                        def train_with():
                            old_model = self.storage.get_model(c_id)
                            training_file = create_jsonl_file(self.storage.get_data(c_id))
                            upload_response = openai.File.create(file=training_file, purpose="fine-tune")
                            training_file.close()
                            fine_tuning_job = openai.FineTune.create(training_file= upload_response.id)

                            print(f"Fine-tuning job created: {fine_tuning_job}", flush=True)
                            global job_id # global state isn't great, but thats interrupt handlers
                            job_id = fine_tuning_job["id"]
                            while True:
                                fine_tuning_status = openai.FineTune.retrieve(id=job_id)
                                status = fine_tuning_status["status"]
                                print(f"Fine-tuning job status: {status}")
                                if status in ["succeeded", "completed", "failed"]:
                                    break
                                time.sleep(30)
                            job_id = None #
                            new_model_id = fine_tuning_status.fine_tuned_model

                            print("New_model_id: ", new_model_id, flush=True)

                            self.storage.set_model(c_id, new_model_id)
                            self.storage.set_training_in_progress(c_id, False)
                            if old_model is not None:
                                openai.Model.delete(old_model)

                        asyncStart(train_with)
                return (best_completion, actual_train)

            best_completion_promise = asyncStart(promiseCompletion)

            if completion is None:
                # crazy story: succeed_train gets set before this anyway if it makes sense to set it!
                completion, succeed_train = asyncAwait(best_completion_promise)
            print(completion)
        

        def succeed_train_closure(use_completion = None):
            def promise():
                if succeed_train is not None:
                    return succeed_train(use_completion)
                if best_completion_promise is not None:
                    try:
                        return asyncAwait(best_completion)[1](use_completion)
                    except:
                        return
            return asyncStart(promise)

        return completion, succeed_train_closure
    
   
    

def create_jsonl_file(data_list):
    out = tempfile.TemporaryFile('w+')
    for a,b in data_list:
        out.write(json.dumps({'prompt': a, 'completion': b}) + "\n")
    out.seek(0)
    return out


