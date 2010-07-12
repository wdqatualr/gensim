#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2010 Radim Rehurek <radimrehurek@seznam.cz>
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html

"""
Module for Latent Semantic Indexing.
"""


import logging
import itertools

import numpy

from gensim import interfaces, matutils, utils


class LsiModel(interfaces.TransformationABC):
    """
    Objects of this class allow building and maintaining a model for Latent 
    Semantic Indexing (also known as Latent Semantic Analysis).
    
    The main methods are:
    
    1. constructor, which initializes the projection into latent topics space,
    2. the ``[]`` method, which returns representation of any input document in the 
       latent space,
    3. the `addDocuments()` method, which allows for incrementally updating the model with new documents. 

    Model persistency is achieved via its load/save methods.
    
    """
    def __init__(self, corpus = None, id2word = None, numTopics = 200, extraDims = 10, 
                 chunks = 100, dtype = numpy.float64):
        """
        `numTopics` is the number of requested factors (latent dimensions). 
        
        After the model has been trained, you can estimate topics for an
        arbitrary, unseen document, using the ``topics = self[document]`` dictionary 
        notation. You can also add new training documents, with ``self.addDocuments``,
        so that training can be stopped and resumed at any time, and the
        LSI transformation is available at any point.

        `extraDims` is the number of extra dimensions that will be internally 
        computed (ie. `numTopics + extraDims`) to improve numerical properties of 
        the SVD algorithm. These extra dimensions will be eventually chopped off
        for the final projection. Set to 0 to save memory; set to ~10 to
        2*numTopics for increased SVD precision.
        
        If you specify a `corpus`, it will be used to train the model. See the 
        method `addDocuments` for a description of the `chunks` and `decay` parameters.
        
        The algorithm is based on
        **Brand, 2006: Fast low-rank modifications of the thin singular value decomposition**.
    
        Example:
        
        >>> lsi = LsiModel(corpus, numTopics = 10)
        >>> print lsi[doc_tfidf]
        >>> lsi.addDocuments(corpus2) # update LSI on additional documents
        >>> print lsi[doc_tfidf]
        
        """
        self.id2word = id2word
        self.numTopics = numTopics # number of latent topics
        self.extraDims = extraDims
        self.dtype = dtype
        
        if corpus is None and self.id2word is None:
            raise ValueError('at least one of corpus/id2word must be specified, to establish input space dimensionality')
        
        if self.id2word is None:
            logging.info("no word id mapping provided; initializing from corpus, assuming identity")
            self.id2word = utils.dictFromCorpus(corpus)
            self.numTerms = len(self.id2word)
        else:
            self.numTerms = 1 + max([-1] + self.id2word.keys())
        
        self.projection = numpy.asmatrix(numpy.zeros((self.numTopics, self.numTerms), dtype = dtype))
        self.u = None
        self.s = numpy.asmatrix(numpy.zeros((self.numTopics + self.extraDims, self.numTopics + self.extraDims)), dtype = dtype)
        self.v = None

        if corpus is not None:
            self.addDocuments(corpus, chunks = chunks, updateProjection = True)
    
    
    def addDocuments(self, corpus, chunks = 100, decay = 1.0, reorth = False, 
                     updateProjection = True):
        """
        Update singular value decomposition factors to take into account a new 
        corpus of documents.
        
        Training proceeds in chunks of `chunks` documents at a time.
        This parameter is a tradeoff between increased speed (bigger `chunks`) vs. 
        lower memory footprint (smaller `chunks`). Default is processing 100 documents
        at a time.

        Setting `decay` < 1.0 causes re-orientation towards new data trends in the 
        input document stream, by giving less emphasis to old observations. This allows
        SVD to gradually "forget" old observations and give more preference to 
        new ones. The decay is applied once after every `chunks` documents.
        
        This function corresponds to the general update of Brand (section 2), 
        specialized for `A = docs.T` and `B` trivial (only append the new columns).
        For a function that supports arbitrary updates (appending columns, erasing 
        columns, column revisions and recentering), see the `svdUpdate` function in
        this module.
        """
        logging.debug("updating SVD with %i new documents" % len(corpus))
        
        # initialize decomposition with old values
        if self.u is None:
            self.u = numpy.matrix(numpy.zeros((self.numTerms, self.numTopics + self.extraDims)), dtype = self.dtype)
            self.u[:self.projection.shape[1], :self.projection.shape[0]] = self.projection.T
            del self.projection # free up memory
        
        # do the actual work -- perform iterative singular value decomposition.
        # this is done by sequentially updating SVD with `chunks` new documents
        chunker = itertools.groupby(enumerate(corpus), key = lambda val: val[0] / chunks)
        for chunkNo, (key, group) in enumerate(chunker):
            # convert the chunk of sparse documents to full vectors
            docs = numpy.asarray([matutils.sparse2full(doc, self.numTerms) for docNo, doc in group])
#            self.svdAddCols(docs, reorth = chunkNo % 100 == 99) # reorthogonalize once in every "100*chunks" documents
            self.svdAddCols(docs, decay = decay, reorth = reorth)
            logging.info("processed documents up to #%s" % docNo)
        
        if updateProjection:
            # calculate projection needed to get the topic-document matrix from 
            # a term-document matrix.
            #
            # the way to represent a vector `x` in latent space is lsi[x] = v = self.s^-1 * self.u^-1 * x,
            # so the projection is self.s^-1 * self.u^-1.
            #
            # the way to compare two documents `x1`, `x2` is to compute v1 * self.s^2 * v2.T, so
            # we pre-multiply v * s (ie., scale axes by singular values), and return
            # that directly as the representation of `x` in LSI space.
            #
            # this conveniently simplifies to lsi[x] = self.u.T * x, so the projection is 
            # just self.u.T
            # 
            # note that neither `v` (the right singular vectors) nor `s` (the singular 
            # values) are used at all in this scaled transformation
            logging.debug("computing transformation projection, clipping extra %i dimensions" %
                          (self.extraDims))
            self.projection = self.u[:, :self.numTopics].T.astype(numpy.float32).copy('C') # make sure we get a row-contiguous array, for fast mat*vec multiplications
            self.u = None # free up memory
            # this whole self.u/self.projection business is meant to save memory,
            # so that we don't need to keep both matrices in the memory, or at least
            # not for long.
            # if you know you will be adding documents multiple times in a row with 
            # addDocuments, with no transformation calls in between, it is more 
            # efficient (both in memory, CPU and numerical precision) to keep 
            # updateProjection=False and only set it to True at the final update.
    
    
    def svdAddCols(self, docs, decay = 1.0, reorth = False):
        """
        If `X = self.u * self.s * self.v^T` is the current decomposition,
        update it so that `self.u * self.s * self.v^T = [X docs.T]`,
        that is, append new columns to the original matrix.
        
        `docs` is a **dense** matrix containing the new observations as rows.
        """
        keepV = self.v is not None
        if not keepV and reorth:
            raise TypeError("cannot reorthogonalize without the right singular vectors (v must not be None)")
        a = numpy.asmatrix(numpy.asarray(docs)).T
        m, k = self.u.shape
        if keepV:
            n, k2 = self.v.shape
            assert k == k2, "left/right singular vectors shape mismatch!"
        m2, c = a.shape
        assert m == m2, "new documents must be in the same term-space as the original documents (old %s, new %s)" % (self.u.shape, a.shape)
        
        # construct orthogonal basis for (I - U * U^T) * A
        logging.debug("constructing orthogonal component")
        m = self.u.T * a # project documents into eigenspace; (k, m) * (m, c) = (k, c)
        logging.debug("computing orthogonal basis")
        P, Ra = numpy.linalg.qr(a - self.u * m) # equation (2)

        # allow re-orientation towards new data trends in the document stream, by giving less emphasis on old values
        self.s *= decay
        
        # now we're ready to construct K; K will be mostly diagonal and sparse, with
        # lots of structure, and of shape only (k + c, k + c), so its direct SVD 
        # ought to be fast for reasonably small additions of new documents (ie. tens 
        # or hundreds of new documents at a time).
        empty = matutils.pad(numpy.matrix([]).reshape(0, 0), c, k)
        K = numpy.bmat([[self.s, m], [empty, Ra]]) # (k + c, k + c), equation (4)
        logging.debug("computing %s SVD" % str(K.shape))
        uK, sK, vK = numpy.linalg.svd(K, full_matrices = False) # there is no python linalg wrapper for partial svd => request all k + c factors :(
        lost = 1.0 - numpy.sum(sK[: k]) / numpy.sum(sK)
        logging.debug("discarding %.1f%% of data variation" % (100 * lost))
        
        # clip full decomposition to the requested rank
        uK = numpy.matrix(uK[:, :k])
        sK = numpy.matrix(numpy.diag(sK[: k]))
        vK = numpy.matrix(vK.T[:, :k]) # .T because numpy transposes the right vectors V, so we need to transpose it back: V.T.T = V
        
        # and finally update the left/right singular vectors
        logging.debug('rotating subspaces')
        self.s = sK
        
        # update U piece by piece, to avoid creating (huge) temporary arrays in a complex expression and running out of memory
        P = P * uK[k:]
        self.u = self.u * uK[:k]
        self.u += P # (m, k) * (k, k) + (m, c) * (c, k) = (m, k), equation (5)
        del P # free up memory
        
        if keepV:
            self.v = self.v * vK[:k, :] # (n + c, k) * (k, k) = (n + c, k)
            rot = vK[k:, :]
            self.v = numpy.bmat([[self.v], [rot]])
            
            if reorth:
                logging.debug("re-orthogonalizing the decomposition")
                uQ, uR = numpy.linalg.qr(self.u)
                vQ, vR = numpy.linalg.qr(self.v)
                uK, sK, vK = numpy.linalg.svd(uR * self.s * vR.T, full_matrices = False)
                uK = numpy.matrix(uK[:, :k])
                sK = numpy.matrix(numpy.diag(sK[: k]))
                vK = numpy.matrix(vK.T[:, :k])
                
                logging.debug("adjusting singular values by %f%%" % 
                              (100.0 * numpy.sum(numpy.abs(self.s - sK)) / numpy.sum(numpy.abs(self.s))))
                self.u = uQ * uK
                self.s = sK
                self.v = vQ * vK
        logging.debug("added %i documents" % len(docs))

    
    def __str__(self):
        return "LsiModel(numTerms=%s, numTopics=%s, extraDims=%s, chunks=%s)" % \
                (self.numTerms, self.numTopics, self.extraDims, self.chunks)


    def __getitem__(self, bow, scaled = True):
        """
        Return latent distribution, as a list of (topic_id, topic_value) 2-tuples.
        
        This is done by folding input document into the latent topic space. 
        
        Note that this function returns the latent space representation **scaled by the
        singular values**. To return non-scaled embedding, set `scaled` to False.
        """
        # if the input vector is in fact a corpus, return a transformed corpus as result
        if utils.isCorpus(bow):
            return self._apply(bow)
        
        vec = matutils.sparse2full(bow, self.numTerms)
        vec.shape = (self.numTerms, 1)
        assert vec.dtype == numpy.float32 and self.projection.dtype == numpy.float32
        topicDist = self.projection * vec
        if not scaled:
            topicDist = numpy.diag(numpy.diag(1.0 / self.s)) * topicDist
        return [(topicId, float(topicValue)) for topicId, topicValue in enumerate(topicDist)
                if numpy.isfinite(topicValue) and not numpy.allclose(topicValue, 0.0)]
    

    def printTopic(self, topicNo, topN = 10):
        """
        Return a specified topic (0 <= `topicNo` < `self.numTopics`) as string in human readable format.
        
        >>> lsimodel.printTopic(10, topN = 5)
        '-0.340 * "category" + 0.298 * "$M$" + 0.183 * "algebra" + -0.174 * "functor" + -0.168 * "operator"'
        
        """
#        c = numpy.asarray(self.u[:, topicNo]).flatten()
        c = numpy.asarray(self.projection[topicNo, :]).flatten()
        norm = numpy.sqrt(numpy.sum(c * c))
        most = numpy.abs(c).argsort()[::-1][:topN]
        return ' + '.join(['%.3f * "%s"' % (1.0 * c[val] / norm, self.id2word[val]) for val in most])
#endclass LsiModel


def svdUpdate(U, S, V, a, b):
    """
    Update SVD of an (m x n) matrix `X = U * S * V^T` so that
    `[X + a * b^T] = U' * S' * V'^T`
    and return `U'`, `S'`, `V'`.
    
    The original matrix X is not needed at all, so this function implements flexible 
    *online* updates to an existing decomposition. 
    
    `a` and `b` are (m, 1) and (n, 1) matrices.
    
    You can set V to None if you're not interested in the right singular
    vectors. In that case, the returned V' will also be None (saves memory).
    
    This is the rank-1 update as described in
    *Brand, 2006: Fast low-rank modifications of the thin singular value decomposition*
    """
    # convert input to matrices (no copies of data made if already numpy.ndarray or numpy.matrix)
    S = numpy.asmatrix(S)
    U = numpy.asmatrix(U)
    if V is not None:
        V = numpy.asmatrix(V)
    a = numpy.asmatrix(a).reshape(a.size, 1)
    b = numpy.asmatrix(b).reshape(b.size, 1)
    
    rank = S.shape[0]
    
    # eq (6)
    m = U.T * a
    p = a - U * m
    Ra = numpy.sqrt(p.T * p)
    if float(Ra) < 1e-10:
        logging.debug("input already contained in a subspace of U; skipping update")
        return U, S, V
    P = (1.0 / float(Ra)) * p
    
    if V is not None:
        # eq (7)
        n = V.T * b
        q = b - V * n
        Rb = numpy.sqrt(q.T * q)
        if float(Rb) < 1e-10:
            logging.debug("input already contained in a subspace of V; skipping update")
            return U, S, V
        Q = (1.0 / float(Rb)) * q
    else:
        n = numpy.matrix(numpy.zeros((rank, 1)))
        Rb = numpy.matrix([[1.0]])    
    
    if float(Ra) > 1.0 or float(Rb) > 1.0:
        logging.debug("insufficient target rank (Ra=%.3f, Rb=%.3f); this update will result in major loss of information"
                      % (float(Ra), float(Rb)))
    
    # eq (8)
    K = numpy.matrix(numpy.diag(list(numpy.diag(S)) + [0.0])) + numpy.bmat('m ; Ra') * numpy.bmat('n ; Rb').T
    
    # eq (5)
    u, s, vt = numpy.linalg.svd(K, full_matrices = False)
    tUp = numpy.matrix(u[:, :rank])
    tVp = numpy.matrix(vt.T[:, :rank])
    tSp = numpy.matrix(numpy.diag(s[: rank]))
    Up = numpy.bmat('U P') * tUp # FIXME: keep the tUp rotations separate ala eq (11)? this would mean discarding every P as soon as we hit the target rank, is that ok?
    if V is not None:
        Vp = numpy.bmat('V Q') * tVp # ditto
    else:
        Vp = None
    Sp = tSp
    
    return Up, Sp, Vp


def iterSvd(corpus, numTerms, numFactors, numIter = 200, initRate = None, convergence = 1e-4):
    """
    Perform iterative Singular Value Decomposition on a streaming corpus, returning 
    `numFactors` greatest factors (ie., not necessarily the full spectrum).
    
    The parameters `numIter` (maximum number of iterations) and `initRate` (gradient 
    descent step size) guide convergency of the algorithm. It requires `numFactors`
    passes over the corpus.
    
    See **Genevieve Gorrell: Generalized Hebbian Algorithm for Incremental Singular 
    Value Decomposition in Natural Language Processing. EACL 2006.**
    
    Use of this function is deprecated; although it works, it is several orders of 
    magnitude slower than the direct (non-stochastic) version based on Brand (which
    operates in a single pass, too) => use svdAddCols/svdUpdate to compute SVD 
    iteratively. I keep this function here purely for backup reasons.
    """
    logging.info("performing incremental SVD for %i factors" % numFactors)

    # define the document/term singular vectors, fill them with a little random noise
    sdoc = 0.01 * numpy.random.randn(len(corpus), numFactors)
    sterm = 0.01 * numpy.random.randn(numTerms, numFactors)
    if initRate is None:
        initRate = 1.0 / numpy.sqrt(numTerms)
        logging.info("using initial learn rate of %f" % initRate)

    rmse = rmseOld = numpy.inf
    for factor in xrange(numFactors):
        learnRate = initRate
        for iterNo in xrange(numIter):
            errors = 0.0
            rate = learnRate / (1.0 + 9.0 * iterNo / numIter) # gradually decrease the learning rate to 1/10 of the initial value
            logging.debug("setting learning rate to %f" % rate)
            for docNo, doc in enumerate(corpus):
                vec = dict(doc)
                if docNo % 10 == 0:
                    logging.debug('PROGRESS: at document %i/%i' % (docNo, len(corpus)))
                vdoc = sdoc[docNo, factor]
                vterm = sterm[:, factor] # create a view (not copy!) of a matrix row
                
                # reconstruct one document, using all previous factors <0..factor-1>
                recon = numpy.dot(sdoc[docNo, :factor], sterm[:, :factor].T)
                
                for termId in xrange(numTerms):
                    # error of one matrix element = real value - reconstructed value
                    error = vec.get(termId, 0.0) - (recon[termId] + vdoc * vterm[termId])
                    errors += error * error
                    
                    # update the singular vectors
                    tmp = vdoc
                    vdoc += rate * error * vterm[termId]
                    vterm[termId] += rate * error * tmp
                sdoc[docNo, factor] = vdoc
            
            # compute rmse = root mean square error of the reconstructed matrix
            rmse = numpy.exp(0.5 * (numpy.log(errors) - numpy.log(len(corpus)) - numpy.log(numTerms)))
            if not numpy.isfinite(rmse) or rmse > rmseOld:
                learnRate /= 2.0 # if we are not converging (oscillating), halve the learning rate
                logging.info("iteration %i diverged; halving the learning rate to %f" %
                             (iterNo, learnRate))
            
            # check convergence, looking for an early exit (but no sooner than 10% 
            # of numIter have passed)
            converged = numpy.divide(numpy.abs(rmseOld - rmse), rmseOld)
            logging.info("factor %i, finished iteration %i, rmse=%f, rate=%f, converged=%f" %
                          (factor, iterNo, rmse, rate, converged))
            if iterNo > numIter / 10 and numpy.isfinite(converged) and converged <= convergence:
                logging.debug("factor %i converged in %i iterations" % (factor, iterNo + 1))
                break
            rmseOld = rmse
        
        logging.info("PROGRESS: finished SVD factor %i/%i, RMSE<=%f" % 
                     (factor + 1, numFactors, rmse))
    
    # normalize the vectors to unit length; also keep the scale
    sdocLens = numpy.sqrt(numpy.sum(sdoc * sdoc, axis = 0))
    stermLens = numpy.sqrt(numpy.sum(sterm * sterm, axis = 0))
    sdoc /= sdocLens
    sterm /= stermLens
    
    # singular value 
    svals = sdocLens * stermLens
    return sterm, svals, sdoc.T


#def stochasticSvd(a, numTerms, numFactors, p = None, q = 0):
#    """
#    SVD decomposition based on stochastic approximation.
#    
#    See **Halko, Martinsson, Tropp. Finding structure with randomness, 2009.**
#    
#    This is the randomizing version with oversampling, but without power iteration. 
#    """
#    k = numFactors
#    if p is None:
#        l = 2 * k # default oversampling
#    else:
#        l = k + p
#    
#    # stage A: construct the "action" basis matrix Q
#    y = numpy.empty(dtype = numpy.float64, shape = (numTerms, l)) # in double precision, because we will be computing orthonormal basis on this possibly ill-conditioned projection
#    for i, row in enumerate(a):
#        y[i] = column_stack(matutils.sparse2full(doc, numTerms) * numpy.random.normal(0.0, 1.0, numTerms)
#                           for doc in corpus)
#    q = numpy.linalg.qr()
#    
    







