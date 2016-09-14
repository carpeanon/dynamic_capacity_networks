
from slim import ops
from slim import scopes
from slim import variables
from slim import losses

import tensorflow as tf

######## Parameters ########
N_PATCHES = 24
############################

def top_layers(inputs):

    with tf.variable_scope('top_layers'):
        out = ops.conv2d(inputs, 96, [4,4], stride=2,padding='VALID', scope='top_conv1')
        _,fm_size,fm_size,_ = out.get_shape()
        out = ops.max_pool(out, [fm_size,fm_size], stride=1, scope='top_gpool')

        out = ops.flatten(out, scope='top_flatten')
        
        # scale parameters are necessary for fc
        bn_out = {'decay': 0.99, 'epsilon': 0.001, 'scale':True}
        out = ops.fc(out, 10, activation=None, batch_norm_params=bn_out, scope='top_logits')

    return out

def coarse_layers(inputs):

    with tf.variable_scope('coarse_layers'):
        out = ops.conv2d(inputs, 12, [7,7], stride=2, padding='VALID', scope='coarse_conv1')
        out = ops.conv2d(out, 24, [3,3], stride=2, padding='VALID', scope='coarse_conv2')

    return out

def fine_layers(inputs):

    with tf.variable_scope('fine_layers'):
        out = ops.conv2d(inputs, 24, [3,3], stride=1, padding='VALID', scope='fine_conv1')
        out = ops.conv2d(out, 24, [3,3], stride=1, padding='VALID', scope='fine_conv2')
        out = tf.pad(out, [[0,0],[1,1],[1,1],[0,0]])

        out = ops.max_pool(out, [2,2], stride=2, scope='fine_pool1')
        
        out = ops.conv2d(out, 24, [3,3], stride=1, padding='VALID', scope='fine_conv3')
        out = tf.pad(out, [[0,0],[1,1],[1,1],[0,0]])
        out = ops.conv2d(out, 24, [3,3], stride=1, padding='VALID', scope='fine_conv4')
        out = tf.pad(out, [[0,0],[1,1],[1,1],[0,0]])

        out = ops.max_pool(out, [2,2], stride=2, scope='fine_pool2')

        out = ops.conv2d(out, 24, [3,3], stride=1, padding='VALID', scope='fine_conv5')

    return out

def entropy(coarse_logits):
    """Calculate the entropy of the coarse model output
    """
    return -tf.reduce_sum(coarse_logits*tf.log(tf.clip_by_value(coarse_logits,1e-10,1.0)))

def identify_saliency(grads):
    """Identify top k saliency scores.

       Args.
            grads: gradient of the entropy wrt features
       Trick.
            use tf.nn.top_k ops to extract position indices
    """

    M = tf.sqrt(tf.reduce_sum(tf.square(grads),3)+1e-8)
    top_k_values, top_k_idxs = tf.nn.top_k(ops.flatten(M), N_PATCHES, sorted=False)

    # shuffle patch indices for batch normalization
    top_k_idxs = tf.random_shuffle(tf.transpose(top_k_idxs))
    top_k_idxs = tf.transpose(top_k_idxs)

    return top_k_values, top_k_idxs, M

def extract_patches(inputs, size, offsets):

    batch_size = inputs.get_shape()[0]

    padded = tf.pad(inputs, [[0,0],[2,2],[2,2],[0,0]])
    unpacked = tf.unpack(tf.squeeze(padded))

    extra_margins = tf.constant([1,1,2,2])

    sliced_list = []
    for i in xrange(batch_size.value):
    
        margins = tf.random_shuffle(extra_margins)
        margins = margins[:2]
        start_pts = tf.sub(offsets[i,:],margins)
        sliced = tf.slice(unpacked[i],start_pts,size)
        sliced_list.append(sliced)

    patches = tf.pack(sliced_list)
    patches = tf.expand_dims(patches,3)

    return patches

    

def extract_features(inputs, k_idxs, map_h):
    """Extract top k fine features

       NOTE.
            do not use tf.image.extract_glimpse ops to get input patches
            (cf. https://github.com/tensorflow/tensorflow/issues/2134)
    """

    def _extract_feature(inputs, idxs):

        idxs = tf.expand_dims(idxs,1)

        idx_i = tf.floordiv(idxs, map_h)
        idx_j = tf.mod(idxs, map_h)

        # NOTE: the below origins are starting points, not center!
        origin_i = 2*(2*idx_i+1)+3 - 5 + 2
        origin_j = 2*(2*idx_j+1)+3 - 5 + 2

        origin_centers = tf.concat(1,[origin_i,origin_j])

        # NOTE: size also depends on the architecture
        #patches = tf.image.extract_glimpse(inputs, size=[14,14], offsets=origin_centers, 
        #                                   centered=False, normalized=False)
        patches = extract_patches(inputs, size=[14,14], offsets=origin_centers)
        
        #fine_features = fine_layers(patches)
        fine_features = []

        src_idxs = tf.concat(1,[idx_i,idx_j])

        return fine_features, src_idxs, patches

    k_features = []
    k_src_idxs = []
    k_patches = []
    for i in xrange(N_PATCHES):
        fine_feature, src_idx, patches = _extract_feature(inputs,k_idxs[:,i])
        k_features.append(fine_feature)
        k_src_idxs.append(src_idx)
        k_patches.append(patches)

    
    concat_patches = tf.concat(0,k_patches)
    concat_k_features = fine_layers(concat_patches)
    k_features = tf.split(0,N_PATCHES,concat_k_features)

    return k_features, k_src_idxs, k_patches


def replace_features(coarse_features, fine_features, replace_idxs):
    """ Replace fine features with the corresponding coarse features

        Trick.
            use tf.dynamic_stitch ops

    """
   
    # TODO: simplify indexing 
    def _convert_to_1d_idxs(src_idxs):
        """ Convert 2D idxs to 1D idxs 
            within 1D tensor whose shape is (b*h*w*c)
        """
        batch_idx_len = map_channel.value * map_width.value * map_height.value
        batch_idx_base = [i*batch_idx_len for i in xrange(batch_size.value)]

        batch_1d = map_channel.value * map_width.value * src_idxs[:,0] + \
                   map_channel.value * src_idxs[:,1]
        batch_1d = tf.add(batch_1d,batch_idx_base)
        
        flat_idxs = [batch_1d+i for i in xrange(map_channel.value)]
        flat_idxs = tf.reshape(tf.transpose(tf.pack(flat_idxs)), [-1])

        return flat_idxs

    batch_size, map_height, map_width, map_channel = coarse_features.get_shape()

    # flatten coarse features
    flat_coarse_features = tf.reshape(coarse_features, [batch_size.value,-1])
    flat_coarse_features = tf.reshape(flat_coarse_features, [-1])


    # flatten fine features
    flat_fine_features = [tf.reshape(i,[-1]) for i in fine_features]
    flat_fine_features = tf.concat(0,flat_fine_features)

    flat_fine_idxs = [_convert_to_1d_idxs(i) for i in replace_idxs]
    flat_fine_idxs = tf.concat(0,flat_fine_idxs)

    # extract coarse features to be replaced
    # this is required for hint-based training
    flat_coarse_replaced = tf.gather(flat_coarse_features, flat_fine_idxs, validate_indices=False)

    merged = tf.dynamic_stitch([tf.range(0,flat_coarse_features.get_shape()[0]),flat_fine_idxs],
            [flat_coarse_features,flat_fine_features])

    merged = tf.reshape(merged,coarse_features.get_shape())

    return merged, flat_coarse_replaced, flat_fine_features

def inference(inputs, is_training=True, scope=''):

    batch_norm_params = {'decay': 0.99, 'epsilon': 0.001}

    with scopes.arg_scope([ops.conv2d, ops.fc], weight_decay=0.0005,
                          is_training=is_training, batch_norm_params=batch_norm_params):
        # get features from coarse layers
        coarse_features = coarse_layers(inputs)
        coarse_features_dim = coarse_features.get_shape()[1] # width

        # calculate saliency scores and extract top k
        coarse_output = top_layers(coarse_features)
        coarse_h = entropy(tf.nn.softmax(coarse_output))
        coarse_grads = tf.gradients(coarse_h, coarse_features, name='gradient_entropy')
        top_k_values, top_k_idxs, M = identify_saliency(coarse_grads[0])

        with tf.control_dependencies([top_k_idxs]):
            top_k_idxs = tf.identity(top_k_idxs)
            coarse_features = tf.identity(coarse_features)
            # get features from fine layers
            fine_features, src_idxs, k_patches = extract_features(inputs, top_k_idxs, coarse_features_dim)

            # merge two feature maps
            merged, flat_coarse, flat_fine = replace_features(coarse_features, fine_features, src_idxs)

            raw_hint_loss = tf.reduce_sum(tf.square(flat_coarse - flat_fine), name='raw_hint_loss')
            # scale hint loss per example in batch
            # still does not match range of 5-25 shown in figure 2 in paper???
            hint_loss = tf.div( raw_hint_loss, inputs.get_shape()[0].value*N_PATCHES, name='objective_hint')
           
            tf.get_variable_scope().reuse_variables()
            final_logits = top_layers(merged)

    return final_logits, hint_loss

def loss(logits, labels, batch_size):

    sparse_labels = tf.reshape(labels, [batch_size,1])
    indices = tf.reshape(tf.range(batch_size), [batch_size,1])
    concated = tf.concat(1, [indices, sparse_labels])
    num_classes = logits.get_shape()[-1].value
    dense_labels = tf.sparse_to_dense(concated, [batch_size, num_classes], 1.0, 0.0)

    losses.cross_entropy_loss(logits, dense_labels, label_smoothing=0.0, weight=1.0)

    


