from .logger import CSVLogger
import logging
logger = logging.getLogger(__name__)
import numpy as np
from collections import OrderedDict
from joblib import Parallel, delayed

from poet_distributed.ppo import PPO, learn_util
from poet_distributed.niches.box2d.env import Env_config
from poet_distributed.reproduce_ops import Reproducer
from poet_distributed.novelty import compute_novelty_vs_archive
import json


def construct_niche_fns_from_env(args, env, seed):
    def niche_wrapper(configs, seed):  # force python to make a new lexical scope
        def make_niche():
            from poet_distributed.niches import Box2DNiche
            return Box2DNiche(env_configs=configs,
                            seed=seed,
                            init=args.init,
                            stochastic=args.stochastic)

        return make_niche

    niche_name = env.name
    configs = (env,)

    return niche_name, niche_wrapper(list(configs), seed)

class MutliPPOOptimizer(object):
    def __init__(self, args):
        self.args = args

        self.env_registry = OrderedDict()
        self.env_archive = OrderedDict()
        self.env_reproducer = Reproducer(args)
        self.optimizers = OrderedDict()
        self.env_seeds = OrderedDict()

        env = Env_config(
                name='flat',
                ground_roughness=0,
                pit_gap=[],
                stump_width=[],
                stump_height=[],
                stump_float=[],
                stair_height=[],
                stair_width=[],
                stair_steps=[])

        self.add_optimizer(env=env, seed=args.master_seed)

    def create_optimizer(self, env, seed, morph_configs, created_at=0, is_candidate=False, parents=None):
        assert env != None

        optim_id, niche_fn = construct_niche_fns_from_env(args=self.args, env=env, seed=seed)

        if morph_configs is not None:
            morph_params = np.array(morph_configs)
        else:
            size = (self.args.init_num_morphs, 8)
            morph_params = np.ones(size, dtype=np.float32)
            for i in range(size[0]):
                length_scale = np.random.uniform(0.25, 1.75)
                width_scale = np.random.uniform(0.25, 1.75)
                morph_params[i, np.array([1, 3, 5, 7])] = length_scale
                morph_params[i, np.array([0, 2, 4, 6])] = width_scale

        num_agents = len(morph_params)

        if parents is None:
            parents = num_agents*[-1]
        ppo_optimizers = []
        for i in range(num_agents):
            ppo_optimizers.append(PPO(
                make_niche=niche_fn,
                morph_params=morph_params[i],
                optim_id=optim_id,
                created_at=created_at,
                is_candidate=is_candidate,
                log_file=self.args.log_file,
                parent=parents[i]))
        
        return ppo_optimizers


    def add_optimizer(self, env, seed, morph_params=None, created_at=0, is_candidate=False, parents=None):
        '''
        add a new env-agent(s) pair
        '''
        opt_list = self.create_optimizer(env, seed, morph_params, created_at, is_candidate, parents)
        optim_id = opt_list[0].optim_id
        self.optimizers[optim_id] = opt_list

        assert optim_id not in self.env_registry.keys()
        assert optim_id not in self.env_archive.keys()
        self.env_registry[optim_id] = env
        self.env_archive[optim_id] = env
        self.env_seeds[optim_id] = seed

        log_file = self.args.log_file
        env_config_file = log_file + '/' + log_file.split('/')[-1] + '.' + optim_id + '_' + str(created_at) + '.env.json'
        record = {'config': env._asdict(), 'seed': seed}
        with open(env_config_file,'w') as f:
            json.dump(record, f)

    def delete_optimizer(self, optim_id):
        assert optim_id in self.optimizers.keys()
        #assume optim_id == env_id for single_env niches
        o = self.optimizers.pop(optim_id)
        del o
        assert optim_id in self.env_registry.keys()
        self.env_registry.pop(optim_id)
        logger.info('DELETED {} '.format(optim_id))

    def pass_dedup(self, env_config):
        if env_config.name in self.env_registry.keys():
            logger.debug("active env already. reject!")
            return False
        else:
            return True

    def pass_mc(self, score):
        if score < self.args.mc_lower or score > self.args.mc_upper:
            return False
        else:
            return True

    def get_new_env(self, list_repro):

        optim_id = self.env_reproducer.pick(list_repro)
        assert optim_id in self.optimizers.keys()
        assert optim_id in self.env_registry.keys()
        parent = self.env_registry[optim_id]
        child_env_config = self.env_reproducer.mutate(parent)

        logger.info("we pick to mutate: {} and we got {} back".format(optim_id, child_env_config.name))
        logger.debug("parent")
        logger.debug(parent)
        logger.debug("child")
        logger.debug(child_env_config)

        seed = np.random.randint(1000000)
        return child_env_config, seed, optim_id

    def remove_oldest(self, num_removals):
        list_delete = []
        for optim_id in self.env_registry.keys():
            if len(list_delete) < num_removals:
                list_delete.append(optim_id)
            else:
                break

        for optim_id in list_delete:
            self.delete_optimizer(optim_id)

    def check_optimizer_status(self):
        logger.info("health_check")
        repro_candidates, delete_candidates = [], []
        for optim_id in self.env_registry.keys():
            opt_list = self.optimizers[optim_id]
            niche_evals = []
            for o in opt_list:
                logger.info("niche {} created at {} start_score {} current_self_evals {}".format(
                    optim_id, o.created_at, o.start_score, o.score))
                niche_evals.append(o.score)
            if np.max(niche_evals) >= self.args.repro_threshold:
                repro_candidates.append(optim_id)

        logger.debug("candidates to reproduce")
        logger.debug(repro_candidates)
        logger.debug("candidates to delete")
        logger.debug(delete_candidates)

        return repro_candidates, delete_candidates

    def evaluate_population_transfer(self, new_opt, optimizers):
        scores = []
        morph_params = []
        for opt in optimizers:
            score = new_opt.eval_agent(opt.actor)
            scores.append(score)
            morph_params.append(opt.morph_params)
        sorted_indices = np.argsort(scores)
        best_scores = np.array(scores)[sorted_indices][-self.args.init_num_morphs:][::-1]
        best_morph_params = np.array(morph_params[sorted_indices][-self.args.init_num_morphs:][::-1])

        return best_scores, best_morph_params
        
    def get_child_list(self, parent_list, max_children):
        child_list = []

        mutation_trial = 0
        while mutation_trial < max_children:
            new_env_config, seed, parent_optim_id = self.get_new_env(parent_list)
            mutation_trial += 1
            if self.pass_dedup(new_env_config):
                morph_params = [x.morph_params for x in self.optimizers[parent_optim_id]]
                opt_list = self.create_optimizer(env=new_env_config, seed=seed, morph_configs=morph_params, is_candidate=True)
                scores = []
                for i in range(len(opt_list)):
                    scores.append(opt_list[i].eval_agent())
                del opt_list
                if self.pass_mc(np.max(scores)):
                    novelty_score = compute_novelty_vs_archive(self.env_archive, new_env_config, k=5)
                    logger.debug("{} passed mc, novelty score {}".format(np.max(scores), novelty_score))
                    child_list.append((new_env_config, seed, parent_optim_id, novelty_score))

        #sort child list according to novelty for high to low
        child_list = sorted(child_list,key=lambda x: x[3], reverse=True)
        return child_list

    def adjust_envs_niches(self, iteration, steps_before_adjust, max_num_envs=None, max_children=8, max_admitted=1):
        if iteration > 0 and iteration % steps_before_adjust == 0:
            list_repro, list_delete = self.check_optimizer_status()
            if len(list_repro) == 0:
                return
            
            logger.info("list of niches to reproduce")
            logger.info(list_repro)
            logger.info("list of niches to delete")
            logger.info(list_delete)

            child_list = self.get_child_list(list_repro, max_children)
            if child_list == None or len(child_list) == 0:
                logger.info("mutation to reproduce env FAILED!!!")
                return
            admitted = 0
            for child in child_list:
                new_env_config, seed, parent_optim_id, _ = child
                morph_params = self.optimizers[parent_optim_id][0].morph_params
                # targeted transfer
                o = self.create_optimizer(new_env_config, seed, morph_params, created_at=iteration, is_candidate=True)[0]
                parent_opts = []
                for opt_list in self.optimizers.values():
                    parent_opts += opt_list
                scores, morph_params = self.evaluate_population_transfer(o, parent_opts)
                parents = []
                for i in range(len(morph_params)):
                    parent = '{}_m_{}'.format(parent_optim_id, '_'.join([str(x) for x in morph_params[i]]))
                    parents.append(parent)
                del o
                if self.pass_mc(np.mean(scores)):
                    self.add_optimizer(env=new_env_config, seed=seed, morph_params=morph_params, created_at=iteration,
                                       is_candidate=False, parents=parents)
                    admitted += 1
                    if admitted >= max_admitted:
                        break
            
            if max_num_envs and len(self.optimizers) > max_num_envs:
                num_removals = len(self.optimizers) - max_num_envs
                self.remove_oldest(num_removals)

    def remove_oldest_agents(self, optim_id, num_removals):
        list_delete = self.optimizers[optim_id][:num_removals]
        for agent in list_delete:
            logger.info("Deleting agent: {} from env: {}".format(agent.morph_id, optim_id))
            self.optimizers[optim_id].remove(agent)

    def add_agents_to_env(self, optim_id, agents):
        num_agents = len(self.optimizers[optim_id])
        if num_agents + len(agents) <= self.args.max_num_morphs:
            self.optimizers[optim_id] += agents
        else:
            num_removals = num_agents + len(agents) - self.args.max_num_morphs
            self.remove_oldest_agents(optim_id, num_removals)
            self.optimizers[optim_id] += agents

    def mutate_morph_params(self, params):
        child_params = np.copy(np.array(params, dtype=np.float32))
        lengthen = np.random.choice(2)
        if lengthen:
            eps = np.random.uniform(1, 2)
        else:
            eps = np.random.uniform(0, 1)
        child_params[1] *= eps
        child_params[3] *= eps
        child_params[5] *= eps
        child_params[7] *= eps

        widen = np.random.choice(2)
        if widen:
            eps = np.random.uniform(1, 2)
        else:
            eps = np.random.uniform(0, 1)
        child_params[0] *= eps
        child_params[2] *= eps
        child_params[4] *= eps
        child_params[6] *= eps

        child_params[child_params > 1.75] = 1.75
        child_params[child_params < 0.25] = 0.25

        return child_params

    def evolve_morphology(self, iteration):
        for optim_id in self.optimizers:
            agents = self.optimizers[optim_id]
            groups = np.random.choice(agents, (len(agents)//4, 4), replace=False)
            fittest_scores = -np.inf*np.ones(len(groups))
            fittest_agents = len(groups) * [None]
            for k, group in enumerate(groups):
                for i in range(len(group)):
                    score = group[i].score
                    if score > fittest_scores[k]:
                        fittest_scores[k] = score
                        fittest_agents[k] = group[i]
                        
            child_morph_params = []
            parents = []
            for agent in fittest_agents:
                parent_morph_params = agent.morph_params
                child_morph = self.mutate_morph_params(parent_morph_params)
                child_morph_params.append(child_morph)
                parents.append('{}_m_{}'.format(optim_id,'_'.join([str(x) for x in child_morph])))

            child_list = self.create_optimizer(env=self.env_registry[optim_id], 
                seed=self.env_seeds[optim_id], morph_configs=child_morph_params, created_at=iteration,
                is_candidate=False, parents=parents)
            self.add_agents_to_env(optim_id, child_list)

    def ind_ppo_step(self, iteration):
        agents = []
        for agent_list in self.optimizers.values():
            agents += agent_list
        Parallel(n_jobs=self.args.num_workers, verbose=51, backend='threading')(
            delayed(learn_util)(agent) for agent in agents
        )
        if iteration == 0:
            for agent in agents:
                agent.start_score = agent.score.copy()

    def optimize(self, iterations=200,
                 steps_before_transfer=25,
                 **kwargs):
        for iteration in range(iterations):
            self.adjust_envs_niches(iteration, self.args.adjust_interval * steps_before_transfer,
                                    max_num_envs=self.args.max_num_envs)
            
            if iteration > 0 and iteration % self.args.morph_evolve_interval == 0:
                self.evolve_morphology(iteration=iteration)
            
            self.ind_ppo_step(iteration=iteration)

            for opt_list in self.optimizers.values():
                for o in opt_list:
                    o.save_to_logger(iteration)